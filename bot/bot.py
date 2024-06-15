from typing import Optional
from itertools import cycle

from ares import AresBot
from ares.consts import ALL_STRUCTURES, WORKER_TYPES, UnitRole
from ares.behaviors.combat import CombatManeuver
from ares.behaviors.combat.group import AMoveGroup
from ares.behaviors.combat.individual import PathUnitToTarget, KeepUnitSafe
from ares.behaviors.macro import SpawnController, ProductionController

from cython_extensions import cy_closest_to, cy_distance_to, cy_pick_enemy_target

from itertools import chain


from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId 
from sc2.ids.ability_id import AbilityId
from sc2.unit import Unit
from sc2.units import Units
from sc2.position import Point2




from bot.speedmining import get_speedmining_positions
from bot.speedmining import split_workers
from bot.speedmining import mine

import numpy as np

class DragonBot(AresBot):
    def __init__(self, game_step_override: Optional[int] = None):
        """Initiate custom bot

        Parameters
        ----------
        game_step_override :
            If provided, set the game_step to this value regardless of how it was
            specified elsewhere
        """
        super().__init__(game_step_override)
        
        self.townhall_saturations = {}               # lists the mineral saturation of townhalls in queues of 40 frames, we consider the townhall saturated if max_number + 1 >= ideal_number
        self.assimilator_age = {}                    # this is here to tackle an issue with assimilator having 0 workers on them when finished, although the building worker is assigned to it
        self.unit_roles = {}                         # dictionary to keep track of the roles of the units
        self.scout_targets = {}                      # dictionary to keep track of scout targets
    
    @property
    def attack_target(self) -> Point2:
        if self.enemy_structures:
            return cy_closest_to(self.start_location, self.enemy_structures).position
        # not seen anything in early game, just head to enemy spawn
        elif self.time < 240.0:
            return self.enemy_start_locations[0]
        # else search the map
        else:
            # cycle through expansion locations
            if self.is_visible(self.current_base_target):
                self.current_base_target = next(self.expansions_generator)

            return self.current_base_target
    
    # Army Compositions
    @property
    def Standard_Army(self) -> dict:
        return {
            UnitTypeId.IMMORTAL: {"proportion": 0.4, "priority": 0},
            UnitTypeId.COLOSSUS: {"proportion": 0.3, "priority": 2},
            UnitTypeId.HIGHTEMPLAR: {"proportion": 0.2, "priority": 1},
            UnitTypeId.ZEALOT: {"proportion": 0.1, "priority": 3},
        }
    
    @property
    def cheese_defense_army(self) -> dict:
        return {
            UnitTypeId.ZEALOT: {"proportion": 0.5, "priority": 0},
            UnitTypeId.STALKER: {"proportion": 0.4, "priority": 1},
            UnitTypeId.ADEPT: {"proportion": 0.1, "priority": 2},
        }

    async def on_start(self) -> None:
        await super(DragonBot, self).on_start()
        
        print("Game started")
        self.client.game_step = 2    
        self.speedmining_positions = get_speedmining_positions(self)
        split_workers(self)   

        self.nexus_creation_times = {nexus.tag: self.time for nexus in self.townhalls.ready}  # tracks the creation time of Nexus

        self.current_base_target = self.enemy_start_locations[0]  # set the target to the enemy start location
 
        
        # Sort the expansion locations by distance to the enemy start location
        self.expansion_locations_list.sort(key=lambda loc: loc.distance_to(self.enemy_start_locations[0]))

        # Use the sorted expansion locations as your scout targets
        self.scout_targets = self.expansion_locations_list
        
        self.natural_expansion: Point2 = await self.get_next_expansion()
        
        self.expansions_generator = cycle(
            [pos for pos in self.expansion_locations_list]
        )

        print("Build Chosen:",self.build_order_runner.chosen_opening)
        

    async def on_step(self, iteration: int) -> None:
        await super(DragonBot, self).on_step(iteration)

        self.resource_by_tag = {unit.tag: unit for unit in chain(self.mineral_field, self.gas_buildings)}

        mine(self, iteration)

        # retrieve all attacking units & scouts
        Main_Army = self.mediator.get_units_from_role(role=UnitRole.ATTACKING)
        Scout = self.mediator.get_units_from_role(role=UnitRole.SCOUTING)
        Warp_Prism = self.mediator.get_units_from_role(role=UnitRole.DROP_SHIP)

        #Checks for cannon rushes or worker rushes
        if self.time < 5*60 and self.townhalls.exists:
            enemy_units_near_bases = self.all_enemy_units.closer_than(30, self.townhalls.center)
            pylons = enemy_units_near_bases.of_type([UnitTypeId.PYLON])
            enemyWorkerUnits = enemy_units_near_bases.of_type([UnitTypeId.PROBE, UnitTypeId.SCV, UnitTypeId.DRONE])
            cannons = enemy_units_near_bases.of_type([UnitTypeId.PHOTONCANNON])

            if pylons.exists or enemyWorkerUnits.amount >= 4 or cannons.exists:
                self.build_order_runner.set_build_completed()
                self.defend_worker_cannon_rush(enemyWorkerUnits, cannons)
        
        
        ## Macro and Army control

        if self.build_order_runner.chosen_opening == "B2GM_Starting_Build" and self.build_order_runner.build_completed:             
            self.Control_Main_Army(Main_Army)
            if Warp_Prism:
                prism_location = Warp_Prism[0].position
                self.register_behavior(SpawnController(self.Standard_Army,spawn_target=prism_location))
            else:
                self.register_behavior(SpawnController(self.Standard_Army))

        #send scount to the enemy base if an observer exists
        if Scout:
            self.Control_Scout(Scout)
        
        # if a Warp Prism exists, send it to follow the main army
        if Warp_Prism:
            self.Warp_Prism_Follower(Warp_Prism, Main_Army)
            
        # Checking if there are 2 high templar to warp in Archons
        if self.units(UnitTypeId.HIGHTEMPLAR).amount >= 2:
            for templar in self.units(UnitTypeId.HIGHTEMPLAR).ready:
                templar(AbilityId.MORPH_ARCHON)

            
    async def on_unit_created(self, unit: Unit) -> None:
        await super(DragonBot, self).on_unit_created(unit)
        # Asign all units to the attacking role using ares unit role system
        typeid: UnitTypeId = unit.type_id
        # don't assign workers or buildings to the attacking role
        if typeid in ALL_STRUCTURES or typeid in WORKER_TYPES:
            return

        # add scouting role to Observer and Drop_Ship role warp prism else add attacking role
        if typeid == UnitTypeId.OBSERVER:
            self.mediator.assign_role(tag=unit.tag, role=UnitRole.SCOUTING)
        elif typeid == UnitTypeId.WARPPRISM:
            self.mediator.assign_role(tag=unit.tag, role=UnitRole.DROP_SHIP)
            unit.move(self.natural_expansion.towards(self.game_info.map_center, 1))
        else:
            self.mediator.assign_role(tag=unit.tag, role=UnitRole.ATTACKING)
            unit.attack(self.natural_expansion.towards(self.game_info.map_center, 1))
        

    async def on_building_construction_complete(self, building):
        await super(DragonBot, self).on_building_construction_complete(building)
        if building.type_id == UnitTypeId.NEXUS:
            self.nexus_creation_times[building.tag] = self.time  # update the creation time when a Nexus is created

        if not building.type_id == UnitTypeId.ASSIMILATOR or UnitTypeId.NEXUS:
            building(AbilityId.RALLY_BUILDING, self.natural_expansion)
    
    # Function to defend against worker rushes and cannon rushes
    async def defend_worker_cannon_rush(self, enemy_probes, enemy_cannons):
        # Select a worker
        if worker := self.mediator.select_worker(target_position=self.start_location):
            self.mediator.assign_role(tag=worker.tag, role=UnitRole.DEFENDING)

        # Retrieve workers with a DEFENDING role
        defending_workers: Units = self.mediator.get_units_from_role(role=UnitRole.DEFENDING, unit_type=UnitTypeId.PROBE)

        # Assign workers to attack enemy probes and cannons
        for probe in enemy_probes:
            if defending_worker := defending_workers.closest_to(probe):
                await defending_worker.attack(probe)

        for cannon in enemy_cannons:
            if defending_worker := defending_workers.closest_to(cannon):
                await defending_worker.attack(cannon)
    
    def Control_Main_Army(self, Main_Army: Units)-> None:
        #declare a new group manvuever
        Main_Army_Actions: CombatManeuver = CombatManeuver()
        target: Point2 = self.attack_target

        #Add amove to the main army
        Main_Army_Actions.add(
            AMoveGroup(
                group=Main_Army,
                group_tags={unit.tag for unit in Main_Army},
                target=target,
            )
        )   
        self.register_behavior(Main_Army_Actions)

    # Function to Control Warp Prism
    def Warp_Prism_Follower(self, Warp_Prism: Units, Main_Army: Units)-> None:
        #declare a new group maneuver
        Warp_Prism_Actions: CombatManeuver = CombatManeuver()

        air_grid: np.ndarray = self.mediator.get_air_grid

        # Warp Prism to morph into Phase Mode if close by, transport mode to follow if no unit is being warped in 
        for prism in Warp_Prism:
            distance_to_center = prism.distance_to(Main_Army.center)
            if distance_to_center < 15:
                if prism.is_idle:
                    prism(AbilityId.MORPH_WARPPRISMPHASINGMODE)
            else:
                not_ready_units = [unit for unit in self.units if not unit.is_ready and unit.distance_to(prism) < 6.5]
                if prism.type_id == UnitTypeId.WARPPRISMPHASING and not not_ready_units:
                    prism(AbilityId.MORPH_WARPPRISMTRANSPORTMODE)

                # Calculate a new target position that is 5 distance units away from Main_Army.center
                direction_vector = (prism.position - Main_Army.center).normalized
                new_target = Main_Army.center + direction_vector * 5
                if prism.type_id == UnitTypeId.WARPPRISM:
                    Warp_Prism_Actions.add(
                        PathUnitToTarget(
                            unit=prism,
                            target=new_target,
                            grid=air_grid,
                            danger_distance=10
                        )
                    )

        self.register_behavior(Warp_Prism_Actions)
    
    
    def Control_Scout(self, Scout: Units)-> None:
        #declare a new group maneuver
        Scout_Actions: CombatManeuver = CombatManeuver()
        # get an air grid for the scout to path on
        air_grid: np.ndarray = self.mediator.get_air_grid

        # Create a list of targets for the scout
        targets = self.expansion_locations_list[:5] + [self.enemy_start_locations[0]]
        
        
        # If there's no current target or the current target is None, set the first target
        if not hasattr(self, 'current_scout_target') or self.current_scout_target is None:
            if targets:
                self.current_scout_target = targets[0]

        #Move scout to the main base to scout unless its in danger
        for unit in Scout:
            
            if unit.shield_percentage < 1:
                Scout_Actions.add(
                KeepUnitSafe(
                    unit=unit,
                    grid=air_grid
                )
                )
            else:
                # If the unit is not in danger, move it to the current target
                if unit.distance_to(self.current_scout_target) < 1:
                    # If the unit has reached its current target, move it to the next target
                    if self.current_scout_target is not None:
                        current_index = targets.index(self.current_scout_target)
                        if current_index + 1 < len(targets):
                            self.current_scout_target = targets[current_index + 1]
                        else:
                            # If the unit has visited all targets, set its current target to None
                            self.current_scout_target = None

                if self.current_scout_target is not None:
                    Scout_Actions.add(
                        PathUnitToTarget(
                            unit=unit,
                            target=self.current_scout_target,
                            grid=air_grid,
                            danger_distance=10
                        )
                    )

        self.register_behavior(Scout_Actions)
    
    async def on_end(self, game_result: Result) -> None:
        await super(DragonBot, self).on_end(game_result)
    

    #
    # async def on_building_construction_complete(self, unit: Unit) -> None:
    #     await super(MyBot, self).on_building_construction_complete(unit)
    #
    #     # custom on_building_construction_complete logic here ...
    #
    
    #
    # async def on_unit_destroyed(self, unit_tag: int) -> None:
    #     await super(MyBot, self).on_unit_destroyed(unit_tag)
    #
    #     # custom on_unit_destroyed logic here ...
    #
    # async def on_unit_took_damage(self, unit: Unit, amount_damage_taken: float) -> None:
    #     await super(MyBot, self).on_unit_took_damage(unit, amount_damage_taken)
    #
    #     # custom on_unit_took_damage logic here ...

    