import math
import numpy as np
import carla

from safebench.scenario.tools.scenario_operation import ScenarioOperation
from safebench.scenario.tools.scenario_utils import calculate_distance_transforms
from safebench.scenario.scenario_manager.carla_data_provider import CarlaDataProvider
from safebench.scenario.scenario_definition.basic_scenario import BasicScenario
from safebench.scenario.tools.scenario_helper import get_location_in_distance_from_wp

from safebench.scenario.scenario_policy.reinforce_continuous import constraint


class DynamicObjectCrossing(BasicScenario):
    """
        Without prior vehicle action involving a vehicle and a cyclist/pedestrian, the ego vehicle is passing through a road,
        and encounters a cyclist/pedestrian crossing the road.
    """

    def __init__(self, world, ego_vehicles, config, adversary_type=False, timeout=60):
        super(DynamicObjectCrossing, self).__init__("DynamicObjectCrossing", ego_vehicles, config, world)
        self._wmap = CarlaDataProvider.get_map()
        self._reference_waypoint = self._wmap.get_waypoint(config.trigger_points[0].location)
        
        # other vehicle parameters
        self._other_actor_target_velocity = 2.5
        self._adversary_type = adversary_type  # flag to select either pedestrian (False) or cyclist (True)
        self._num_lane_changes = 1
        
        # Note: transforms for walker and blocker
        self.transform = None
        self.transform2 = None
        self.timeout = timeout
        self._trigger_location = config.trigger_points[0].location
        self._number_of_attempts = 20  # Total Number of attempts to relocate a vehicle before spawning
        self._spawn_attempted = 0  # Number of attempts made so far

        self.scenario_operation = ScenarioOperation(self.ego_vehicles, self.other_actors)
        self.trigger_distance_threshold = 20
        self.actor_type_list.append('walker.*')
        self.actor_type_list.append('static.prop.vendingmachine')
        self.ego_max_driven_distance = 150

    def convert_actions(self, actions):
        yaw_scale = 60
        yaw_mean = 0

        d_min = 15
        d_max = 50
        d_scale = (d_max - d_min) / 2
        dist_mean = (d_max + d_min)/2

        y = constraint(actions[0], -1, 1) / 2 + 0.5
        yaw = constraint(actions[1], -1, 1) * yaw_scale + yaw_mean
        dist = constraint(actions[2], -1, 1) * d_scale + dist_mean

        return [y, yaw, dist]

    def _calculate_base_transform(self, _start_distance, waypoint):
        lane_width = waypoint.lane_width

        # Patches false junctions
        if self._reference_waypoint.is_junction:
            stop_at_junction = False
        else:
            stop_at_junction = True

        location, _ = get_location_in_distance_from_wp(waypoint, _start_distance, stop_at_junction)
        waypoint = self._wmap.get_waypoint(location)
        offset = {"orientation": 270, "position": 90, "z": 0.6, "k": 1.0}
        position_yaw = waypoint.transform.rotation.yaw + offset['position']
        orientation_yaw = waypoint.transform.rotation.yaw + offset['orientation']
        offset_location = carla.Location(
            offset['k'] * lane_width * math.cos(math.radians(position_yaw)),
            offset['k'] * lane_width * math.sin(math.radians(position_yaw))
        )
        location += offset_location
        location.z = self._trigger_location.z + offset['z']
        return carla.Transform(location, carla.Rotation(yaw=orientation_yaw)), orientation_yaw

    def _spawn_blocker(self, transform, orientation_yaw):
        """
            Spawn the blocker prop that blocks the vision from the egovehicle of the jaywalker
        """
        # static object transform
        shift = 0.9
        x_ego = self._reference_waypoint.transform.location.x
        y_ego = self._reference_waypoint.transform.location.y
        x_cycle = transform.location.x
        y_cycle = transform.location.y
        x_static = x_ego + shift * (x_cycle - x_ego)
        y_static = y_ego + shift * (y_cycle - y_ego)
        spawn_point_wp = self.ego_vehicles[0].get_world().get_map().get_waypoint(transform.location)

        #Note: if need to change tranform for blocker, here
        self.transform2 = carla.Transform(
            carla.Location(x_static, y_static, spawn_point_wp.transform.location.z + 0.3),
            carla.Rotation(yaw=orientation_yaw + 180)
        )

    def initialize_actors(self):
        """
            Set a blocker that blocks ego's view on the walker
            Request a walker walk through the street when ego come
        """
        y, yaw, self.trigger_distance_threshold = self.actions  # [0, 1], [-60, 60], [15, 50]

        # cyclist transform
        _start_distance = 45
        # We start by getting and waypoint in the closest sidewalk.
        waypoint = self._reference_waypoint

        while True:
            wp_next = waypoint.get_right_lane()
            self._num_lane_changes += 1
            if wp_next is None or wp_next.lane_type == carla.LaneType.Sidewalk:
                break
            elif wp_next.lane_type == carla.LaneType.Shoulder:
                # Filter Parkings considered as Shoulders
                if wp_next.lane_width > 2:
                    _start_distance += 1.5
                    waypoint = wp_next
                break
            else:
                _start_distance += 1.5
                waypoint = wp_next

        while True:  # We keep trying to spawn avoiding props
            try:
                self.transform, orientation_yaw = self._calculate_base_transform(_start_distance, waypoint)
                forward_vector = self.transform.rotation.get_forward_vector() * y * self._reference_waypoint.lane_width
                self.transform.location += forward_vector
                yaw = self.transform.rotation.yaw + yaw
                if yaw < 0:
                    yaw += 360
                if yaw > 360:
                    yaw -= 360
                self.transform = carla.Transform(
                    self.transform.location,
                    carla.Rotation(self.transform.rotation.pitch, yaw, self.transform.rotation.roll)
                )
                orientation_yaw = yaw
                self._spawn_blocker(self.transform, orientation_yaw)
                break
            except RuntimeError as r:
                # We keep retrying until we spawn
                print("Base transform is blocking objects ", self.transform)
                _start_distance += 0.4
                self._spawn_attempted += 1
                if self._spawn_attempted >= self._number_of_attempts:
                    raise r

        # Now that we found a possible position we just put the vehicle to the underground
        disp_transform = carla.Transform(
            carla.Location(self.transform.location.x, self.transform.location.y, self.transform.location.z),
            self.transform.rotation
        )
        prop_disp_transform = carla.Transform(
            carla.Location(self.transform2.location.x, self.transform2.location.y, self.transform2.location.z),
            self.transform2.rotation
        )

        self.other_actor_transform.append(disp_transform)
        self.other_actor_transform.append(prop_disp_transform)
        self.scenario_operation.initialize_vehicle_actors(self.other_actor_transform, self.other_actors, self.actor_type_list)
        self.reference_actor = self.other_actors[0]

    def create_behavior(self, scenario_init_action):
        self.actions = self.convert_actions(scenario_init_action)

    def update_behavior(self, scenario_action):
        assert scenario_action is None, f'{self.name} should receive [None] action. A wrong scenario policy is used.'
        
        # the walker starts crossing the road
        self.scenario_operation.walker_go_straight(self._other_actor_target_velocity, 0)

    def check_stop_condition(self):
        lane_width = self._reference_waypoint.lane_width
        lane_width = lane_width + (1.25 * lane_width * self._num_lane_changes)
        cur_distance = calculate_distance_transforms(CarlaDataProvider.get_transform(self.other_actors[0]), self.transform)
        if cur_distance > 0.6 * lane_width:
            return True
        return False
