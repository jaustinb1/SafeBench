from __future__ import print_function

import math

import carla

from safebench.scenario.scenario_manager.carla_data_provider import CarlaDataProvider
from safebench.scenario.scenario_definition.basic_scenario import BasicScenario, SpawnOtherActorError

from safebench.scenario.tools.scenario_operation import ScenarioOperation
from safebench.scenario.tools.scenario_helper import (
    generate_target_waypoint,
    generate_target_waypoint_in_route,
    get_crossing_point,
    get_junction_topology
)

from safebench.scenario.scenario_policy.reinforce_continuous import constraint


def get_opponent_transform(added_dist, waypoint, trigger_location):
    """
    Calculate the transform of the adversary
    """
    lane_width = waypoint.lane_width

    offset = {"orientation": 270, "position": 90, "k": 1.0}
    # offset = {"orientation": 270, "position": 190, "k": 1.0}
    _wp = waypoint.next(added_dist)
    if _wp:
        _wp = _wp[-1]
    else:
        raise RuntimeError("Cannot get next waypoint !")

    location = _wp.transform.location
    orientation_yaw = _wp.transform.rotation.yaw + offset["orientation"]
    position_yaw = _wp.transform.rotation.yaw + offset["position"]

    offset_location = carla.Location(
        offset['k'] * lane_width * math.cos(math.radians(position_yaw)),
        offset['k'] * lane_width * math.sin(math.radians(position_yaw)))
    location += offset_location
    location.x = trigger_location.x + 20
    location.z = trigger_location.z
    transform = carla.Transform(location, carla.Rotation(yaw=orientation_yaw))

    return transform


def get_right_driving_lane(waypoint):
    """
        Gets the driving / parking lane that is most to the right of the waypoint as well as the number of lane changes done
    """

    lane_changes = 0
    while True:
        wp_next = waypoint.get_right_lane()
        lane_changes += 1

        if wp_next is None or wp_next.lane_type == carla.LaneType.Sidewalk:
            break
        elif wp_next.lane_type == carla.LaneType.Shoulder:
            # Filter Parkings considered as Shoulders
            if is_lane_a_parking(wp_next):
                lane_changes += 1
                waypoint = wp_next
            break
        else:
            waypoint = wp_next

    return waypoint, lane_changes


def is_lane_a_parking(waypoint):
    """
        This function filters false negative Shoulder which are in reality Parking lanes.
        These are differentiated from the others because, similar to the driving lanes,
        they have, on the right, a small Shoulder followed by a Sidewalk.
    """

    # Parking are wide lanes
    if waypoint.lane_width > 2:
        wp_next = waypoint.get_right_lane()

        # That are next to a mini-Shoulder
        if wp_next is not None and wp_next.lane_type == carla.LaneType.Shoulder:
            wp_next_next = wp_next.get_right_lane()

            # Followed by a Sidewalk
            if wp_next_next is not None and wp_next_next.lane_type == carla.LaneType.Sidewalk:
                return True

    return False


class VehicleTurningRoute(BasicScenario):
    """
        The ego vehicle is passing through a road and encounters a cyclist after taking a turn. 
    """

    def __init__(self, world, ego_vehicles, config, timeout=60):
        super(VehicleTurningRoute, self).__init__("VehicleTurningRoute", ego_vehicles, config, world)
        self.timeout = timeout

        self.running_distance = 10

        self.scenario_operation = ScenarioOperation(self.ego_vehicles, self.other_actors)
        self.actor_type_list.append('vehicle.diamondback.century')

        self.reference_actor = None
        self.trigger_distance_threshold = 20
        self.ego_max_driven_distance = 180

    def convert_actions(self, actions, x_scale, y_scale, x_mean, y_mean):
        yaw_min = 0
        yaw_max = 360
        yaw_scale = (yaw_max - yaw_min) / 2
        yaw_mean = (yaw_max + yaw_min) / 2

        d_min = 10
        d_max = 50
        d_scale = (d_max - d_min) / 2
        dist_mean = (d_max + d_min) / 2

        x = constraint(actions[0], -1, 1) * x_scale + x_mean
        y = constraint(actions[1], -1, 1) * y_scale + y_mean
        yaw = constraint(actions[2], -1, 1) * yaw_scale + yaw_mean
        dist = constraint(actions[3], -1, 1) * d_scale + dist_mean

        return [x, y, yaw, dist]

    def initialize_actors(self):
        cross_location = get_crossing_point(self.ego_vehicles[0])
        cross_waypoint = CarlaDataProvider.get_map().get_waypoint(cross_location)
        entry_wps, exit_wps = get_junction_topology(cross_waypoint.get_junction())
        assert len(entry_wps) == len(exit_wps)
        x = y = 0
        max_x_scale = max_y_scale = 0
        for i in range(len(entry_wps)):
            x += entry_wps[i].transform.location.x + exit_wps[i].transform.location.x
            y += entry_wps[i].transform.location.y + exit_wps[i].transform.location.y
        x /= len(entry_wps) * 2
        y /= len(entry_wps) * 2
        for i in range(len(entry_wps)):
            max_x_scale = max(max_x_scale, abs(entry_wps[i].transform.location.x - x), abs(exit_wps[i].transform.location.x - x))
            max_y_scale = max(max_y_scale, abs(entry_wps[i].transform.location.y - y), abs(exit_wps[i].transform.location.y - y))
        max_x_scale *= 0.8
        max_y_scale *= 0.8
        center_transform = carla.Transform(carla.Location(x=x, y=y, z=0), carla.Rotation(pitch=0, yaw=0, roll=0))
        x_mean = x
        y_mean = y

        x, y, yaw, self.trigger_distance_threshold = self.convert_actions(self.actions, max_x_scale, max_y_scale, x_mean, y_mean)
        _other_actor_transform = carla.Transform(carla.Location(x, y, 0), carla.Rotation(yaw=yaw))
        self.other_actor_transform.append(_other_actor_transform)
        try:
            self.scenario_operation.initialize_vehicle_actors(self.other_actor_transform, self.other_actors, self.actor_type_list)
        except:
            raise SpawnOtherActorError

        self.reference_actor = self.other_actors[0]

    def create_behavior(self, scenario_init_action):
        self.actions = scenario_init_action

    def update_behavior(self, scenario_action):
        assert scenario_action is None, f'{self.name} should receive [None] action. A wrong scenario policy is used.'
        for i in range(len(self.other_actors)):
            cur_actor_target_speed = 10
            self.scenario_operation.go_straight(cur_actor_target_speed, i)

    def check_stop_condition(self):
        return False
