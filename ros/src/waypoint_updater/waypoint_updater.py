#!/usr/bin/env python

import rospy
from std_msgs.msg import Int32, Bool
from geometry_msgs.msg import PoseStamped, TwistStamped
from styx_msgs.msg import Lane, Waypoint

import math
import csv
import os

'''
This node will publish waypoints from the car's current position to some `x` distance ahead.

As mentioned in the doc, you should ideally first implement a version which does not care
about traffic lights or obstacles.

Once you have created dbw_node, you will update this node to use the status of traffic lights too.

Please note that our simulator also provides the exact location of traffic lights and their
current status in `/vehicle/traffic_lights` message. You can use this message to build this node
as well as to verify your TL classifier.
'''

# Parameters to configure the WaypointUpdater class
LOOKAHEAD_WPS = 40  # 25  # Number of waypoints to be published. You can change this number
PLAN_ACCELERATION = 1.0  # Acceleration used for waypoint planning if OVERRIDE_ACCELERATION == True
PLAN_DECELERATION = -5.0  # -1.0 # Deceleration used for waypoint planning if OVERRIDE_ACCELERATION == True
OVERRIDE_ACCELERATION = False  # If False use dbw.launch (site) or dbw_sim.launch (simulation) parameters accel_limit (site: 1.0 m/s2, sim: 1.0 m/s2) and decel_limit (site: -1.0 m/s2, sim: -5.0 m/s2) instead of PLAN_ACCELERATION and PLAN_DECELERATION
OVERRIDE_VELOCITY = None  # Use given (OVERRIDE_VELOCITY = None) or own (OVERRIDE_VELOCITY = target velocity in m/s)
PLAN_ON_CURRENT_VELOCITY = False  # Use current car-velocity for trajectory planning instead of continuous trajectory
NUM_WP_STOP_AFTER_STOPLINE = 2  # 1  # Some tolerance if we did not stop before the stop-line
NUM_WP_STOP_BEFORE_STOPLINE = 1  # 1  # Stop a little bit before the stop-line
DEBUG_WAYPOINTS_CSV = False  # Activate/Deactivate node debug outputs via csv (True, False)
DEBUG_WAYPOINTS_LOG = False  # Activate/Deactivate node debug outputs via console (True, False)
MIN_WAYPOINT_SPEED_ACC = 0.1  # Minimum speed for a waypoint during acceleration (avoid deadlocks)


# Class definition for the waypoint_updater node
class WaypointUpdater(object):

    def __init__(self):

        # Initialize client node and register it with the master
        rospy.init_node('waypoint_updater')

        # Define subscribers to enable the client node to read messages from topics
        # rospy.Subscriber("/topic_name", message_type, callback_function)
        # Each time a message of message_type on topic /topic_name is received,
        # it is passed as an argument to callback_function.
        rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)
        rospy.Subscriber('/traffic_waypoint', Int32, self.traffic_cb)
        rospy.Subscriber('/obstacle_waypoint', Int32, self.obstacle_cb)

        # Subscription for trajectory planning (start value) and debugging
        rospy.Subscriber('/current_velocity', TwistStamped, self.current_velocity_callback, queue_size=2)
        rospy.Subscriber('/vehicle/dbw_enabled', Bool, self.dbw_enabled_callback, queue_size=1)

        # Declare publishers to generate and send node outputs
        # rospy.Publisher("/topic_name", message_type, queue_size=10)
        # publish message of message_type on topic /topic_name with a given queue_size
        # (maximum number messages that may be stored in the publisher queue before messages are dropped)
        self.final_waypoints_pub = rospy.Publisher('final_waypoints', Lane, queue_size=1)
        self.current_waypoint_pub = rospy.Publisher('/current_waypoint', Int32, queue_size=1)

        # Member variables of the WaypointUpdater class
        self.waypoints = None  # global map waypoints initially loaded and stored
        self.waypoint_distances = None  # initially pre calculated distances between global waypoints
        self.last_closest_wp = None  # index of closest waypoint to car position from last cycle
        self.last_next_wp = None  # index of next waypoint from last cycle (first waypoint of last trajectory)
        self.car_pose = None  # car position (in m) and orientation data (in rad)
        self.linear_velocity = None  # car longitudinal velocity in m/s
        self.angular_velocity = None  # car yaw rate in rad/s
        self.red_light_wp = -1  # index of the waypoint for nearest upcoming red light's stop line
        self.object_wp = -1  # index of the waypoint for nearest frontal object (allowance)
        self.force_update = True  # control variable to force cyclic waypoint updates
        self.dbw_enabled = False

        # Maximum allowed velocity as target velocity
        if OVERRIDE_VELOCITY is None:
            # Get target velocity from parameter server (waypoint_loader parameter in waypoint_loader.launch)
            # rospy.get_param('~parameter_name', default_value)
            # '~' is the private namespace qualifier, and indicates that the parameter we wish to get
            # is within this node's private namespace
            # The second parameter is the default value to be returned, in the case that rospy.get_param()
            # was unable to get the parameter from the param server
            self.velocity = rospy.get_param('/waypoint_loader/velocity')
            # Convert target velocity value to SI units (m/s)
            # velocity parameter given in km/h (see waypoint_loader.py)
            self.velocity = (self.velocity * 1000.) / (60. * 60.)
        else:
            # Set target velocity according to own velocity parameter
            self.velocity = OVERRIDE_VELOCITY

        # Target accelerations used for planning
        if OVERRIDE_ACCELERATION is False:
            # Use dbw acceleration parameter values (m/s2)
            self.plan_acceleration = rospy.get_param('/dbw_node/accel_limit')
            self.plan_deceleration = rospy.get_param('/dbw_node/decel_limit') * 0.8  # don't use full potential of
            # deceleration for planning, controller might need some of the potential
        else:
            # Use own acceleration parameter values (m/s2)
            self.plan_acceleration = PLAN_ACCELERATION
            self.plan_deceleration = PLAN_DECELERATION * 0.8  # don't use full potential of
            # deceleration for planning, controller might need some of the potential

        # **********************************************************
        # Debug logging output
        # **********************************************************
        # Console output
        if DEBUG_WAYPOINTS_LOG:
            print('***********************************************************')
            rospy.loginfo('WaypointUpdater initializing.')
            print("-------> LOOKAHEAD_WPS               : {}".format(LOOKAHEAD_WPS))
            print("-------> NUM_WP_STOP_AFTER_STOPLINE  : {}".format(NUM_WP_STOP_AFTER_STOPLINE))
            print("-------> NUM_WP_STOP_BEFORE_STOPLINE : {}".format(NUM_WP_STOP_BEFORE_STOPLINE))
            print("-------> PLAN_ON_CURRENT_VELOCITY    : {}".format(PLAN_ON_CURRENT_VELOCITY))
            print("-------> self.velocity               : {}".format(self.velocity))
            print("-------> self.plan_acceleration      : {}".format(self.plan_acceleration))
            print("-------> self.plan_deceleration      : {}".format(self.plan_deceleration))
            print('***********************************************************')
        # File output (csv)
        if DEBUG_WAYPOINTS_CSV:
            # Define relevant csv fields
            self.csv_fields = ['time', 'x', 'y', 'v_target', 'v', 'psi_p', 'next_wp', 'lookahead_wp', 'stop_wp', 'traj_stop_wp',
                               'traj_waypoints_velx', 'traj_distances', 'traj_waypoints_posx', 'traj_waypoints_posy']
            base_path = os.path.dirname(os.path.abspath(__file__))  # path of waypoint_updater.py
            base_path = os.path.dirname(base_path)  # one path upwards
            base_path = os.path.dirname(base_path)  # one path upwards
            base_path = os.path.dirname(base_path)  # one path upwards
            base_path = os.path.join(base_path, 'data', 'records')
            if not os.path.exists(base_path):
                os.makedirs(base_path)
            csv_file = os.path.join(base_path, 'Debug_Waypoint_Updater.csv')
            self.fid = open(csv_file, 'w')
            self.csv_writer = csv.DictWriter(self.fid, fieldnames=self.csv_fields)
            self.csv_writer.writeheader()
            self.csv_data = {key: 0.0 for key in self.csv_fields}
            rospy.logwarn("Create logfile for waypoint debugging: " + self.fid.name)

        # Block until a shutdown request is received by the node
        rospy.spin()

    # Callback to set current self.car_pose variable
    # for incoming message msg on subscribed topic
    # (rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb))
    def pose_cb(self, msg):
        # car_pose.position.x/y/z
        # car_pose.orientation.x/y/z/w
        self.car_pose = msg.pose

        # Start publishing relevant waypoints when global map data is available
        # (initial subscription successful)
        if self.waypoints is not None:
            # map data available
            self.publish_final_waypoints()

    # Callback to set current self.linear_velocity and self.angular_velocity variable
    # for incoming message msg on subscribed topic
    # (rospy.Subscriber('/current_velocity', TwistStamped, self.current_velocity_callback, queue_size=2))
    def current_velocity_callback(self, data):
        self.linear_velocity = data.twist.linear.x
        self.angular_velocity = data.twist.angular.z

    def dbw_enabled_callback(self, tf):
        self.dbw_enabled = tf
        self.force_update |= not self.dbw_enabled

    # Callback to set current self.waypoints variable for incoming message static_lane on subscribed topic
    # (rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb))
    def waypoints_cb(self, static_lane):
        # Initially called once to set the static waypoint variable self.waypoints for the track (len = 10902)
        if self.waypoints is None:
            # waypoints.pose.pose.position.x/y/z
            # waypoints.pose.pose.orientation.x/y/z/w
            # waypoints.twist.twist.linear.x/y/z
            # waypoints.twist.twist.angular.x/y/z
            self.waypoints = static_lane.waypoints  # initialize global waypoints
            self.update_distances()  # initialize waypoint distances
            self.last_closest_wp = None  # initialize closest waypoint to car position
            self.last_next_wp = None

    # Callback to set current self.red_light_wp variable
    # for incoming message msg on subscribed topic
    # (rospy.Subscriber('/traffic_waypoint', Int32, self.traffic_cb))
    def traffic_cb(self, msg):
        # Iteratively called to set the waypoint for a red traffic light's stop line
        if msg.data != self.red_light_wp:
            # changed traffic light detection
            change_in_lookahead = self.red_light_wp - self.last_next_wp < LOOKAHEAD_WPS or \
                                  msg.data - self.last_next_wp < LOOKAHEAD_WPS

            # traffic light is within our lookahead horizon, force update of trajectory
            self.force_update |= change_in_lookahead
            # update internal traffic-light state
            self.red_light_wp = msg.data

    # Callback to set current self.object_wp variable
    # for incoming message msg on subscribed topic
    # (rospy.Subscriber('/obstacle_waypoint', Int32, self.obstacle_cb))
    def obstacle_cb(self, msg):
        # TODO: Callback for /obstacle_waypoint message. We will not implement this ...
        if msg.data != self.object_wp:
            # changed object detection
            change_in_lookahead = self.object_wp - self.last_next_wp < LOOKAHEAD_WPS or \
                                  msg.data - self.last_next_wp < LOOKAHEAD_WPS

            # object is within lookahead horizon
            self.force_update |= change_in_lookahead
            # update internal object waypoint
            self.object_wp = msg.data

    # Helper function to get the velocity value for a given waypoint in the waypoints vector
    def get_waypoint_velocity(self, waypoint):
        return waypoint.twist.twist.linear.x

    # Helper function to set the velocity value for a given waypoint in the waypoints vector
    # def set_waypoint_velocity(self, waypoints, waypoint, velocity):
    #    waypoints[waypoint].twist.twist.linear.x = velocity
    def set_waypoint_velocity(self, waypoints, i_waypoint, velocity):
        waypoints[i_waypoint].twist.twist.linear.x = velocity

    # Helper function to calculate the distance between two 3D (x,y,z) points a and b
    def distance(self, a, b):
        return math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2 + (a.z-b.z)**2)

    # Helper function to calculate the waypoint_distances between points in the waypoints vector
    def update_distances(self):
        self.waypoint_distances = []

        for i in range(1, len(self.waypoints)):
            a = self.waypoints[i-1].pose.pose.position
            b = self.waypoints[i].pose.pose.position
            delta = self.distance(a, b)
            self.waypoint_distances.append(delta)

    # Helper function to find the closest waypoint to the current vehicle position in the global waypoints vector
    def closest_waypoint(self):
        """
        finds the closest waypoint to our current position.
        To speed things up the search starts from the last known waypoint if possible.
        The search is stopped once the waypoint-distances start increasing again.
        """

        # Ego car position in map-coordinates
        car_x = self.car_pose.position.x
        car_y = self.car_pose.position.y

        # Initialize search
        num_waypoints = len(self.waypoints)
        # The waypoint the ego vehicle was closest to in the last cycle
        if self.last_closest_wp is None:
            # last waypoint not known, start in the middle of the track
            self.last_closest_wp = int(num_waypoints / 2)
            # force complete search
            test_all = True
        else:
            # Relative search with possible early exit
            test_all = False

        def dist_squared(i_wp):
            """ returns the squared distance to waypoint[i]"""
            dx = self.waypoints[i_wp].pose.pose.position.x - car_x
            dy = self.waypoints[i_wp].pose.pose.position.y - car_y
            return dx**2 + dy**2

        # Initialize minimum distance with distance to last_car_waypoint
        index = self.last_closest_wp
        d_min = dist_squared(index)

        # Force check of all waypoints in case we are way off our last known position
        test_all = test_all or d_min > 10**2

        # Search waypoints ahead
        for k in range(self.last_closest_wp + 1, num_waypoints):
            d_k = dist_squared(k)
            if d_k < d_min:
                index = k
                d_min = d_k
            elif not test_all:
                break

        # Search previous waypoints
        for k in range(self.last_closest_wp - 1, -1, -1):
            d_k = dist_squared(k)
            if d_k < d_min:
                index = k
                d_min = d_k
            elif not test_all:
                break

        self.last_closest_wp = index
        return index

    # Helper function that outputs the next waypoint to the current vehicle position
    # in the global waypoints vector (in driving direction)
    def next_waypoint(self):
        """
        returns next waypoint ahead of us
        Python copy of the Udacity code from Path-Planning project
        :param waypoints:
        :param pose:
        :return:
        """

        # Determine the closest waypoint to the current vehicle position with the implemented helper function
        i_closest_wp = self.closest_waypoint()

        # Global map position of the closest waypoint to the ego vehicle
        map_x = self.waypoints[i_closest_wp].pose.pose.position.x
        map_y = self.waypoints[i_closest_wp].pose.pose.position.y

        # Ego vehicle position in global map coordinates
        car_x = self.car_pose.position.x
        car_y = self.car_pose.position.y

        # Calculate the heading angle of vector between car and closest waypoint
        # (The result is between -pi and pi)
        heading = math.atan2(map_y - car_y, map_x - car_x)

        # Transform the quaternion to get the ego yaw angle (between -pi and pi)
        # https://stackoverflow.com/questions/5782658/extracting-yaw-from-a-quaternion
        q = self.car_pose.orientation  # (x, y, z, w)
        yaw = math.atan2(2.0*(q.y*q.z + q.w*q.x), q.w*q.w - q.x*q.x - q.y*q.y + q.z*q.z)

        # Check angle and go to next waypoint if necessary
        angle = abs(yaw - heading)  # absolute angle difference (between 0 and 2pi)
        angle = min(2*math.pi - angle, angle)  # between 0 and pi

        # Select next waypoint in driving direction (ahead of ego vehicle)
        # if currently closest waypoint is behind ego vehicle
        i_next_wp = i_closest_wp
        if angle > math.pi / 4.0:
            i_next_wp = i_closest_wp + 1
            if i_next_wp == len(self.waypoints):
                i_next_wp = 0

        return i_next_wp

    # Helper function that publishes the next waypoint to the current vehicle position
    # (publisher called in callback pose_cb when relevant ego pose data is available)
    def publish_final_waypoints(self):

        if self.waypoints is None or self.car_pose is None:
            # Early exit due to missing data
            if DEBUG_WAYPOINTS_LOG:
                rospy.loginfo("Early exit due to missing data: self.waypoints = {}, self.car_pose = {}".format(self.waypoints, self.car_pose))
            return

        # Get next waypoint ID with helper function
        next_wp = self.next_waypoint()

        # check if we crossed a waypoint
        same_wp = self.last_next_wp is not None and self.last_next_wp == next_wp

        if same_wp and not self.force_update:
            # no update of waypoints required
            pass
        else:
            self.last_next_wp = next_wp

        # Publish waypoint behind of us as current one
        self.current_waypoint_pub.publish(Int32(next_wp-1))  # -1 has to be interpreted correctly

        # Set current lookahead waypoint index based on parameter LOOKAHEAD_WPS
        lookahead_wp = next_wp + LOOKAHEAD_WPS

        # Get next stop waypoint, either end of track or red-light
        num_waypoints = len(self.waypoints)  # total number of given global waypoints for track
        if lookahead_wp >= num_waypoints - 1:
            # End of track in lookahead horizon
            lookahead_wp = num_waypoints  # lookahead waypoint set to end of track
            if next_wp-NUM_WP_STOP_AFTER_STOPLINE <= self.red_light_wp < lookahead_wp:
                # Stop before red stop light in lookahead horizon before end of track
                stop_wp = self.red_light_wp - NUM_WP_STOP_BEFORE_STOPLINE
                stop_wp = max(stop_wp, next_wp)  # next_wp for next_wp >= stop_wp
            else:
                # Stop before end of track
                stop_wp = lookahead_wp - NUM_WP_STOP_BEFORE_STOPLINE
                stop_wp = max(next_wp, stop_wp)

        else:
            # End of track not yet in lookahead horizon (lookahead_wp < num_waypoints - 1)
            if next_wp-NUM_WP_STOP_AFTER_STOPLINE <= self.red_light_wp < lookahead_wp:
                # Stop before red stop light in lookahead horizon
                stop_wp = self.red_light_wp - NUM_WP_STOP_BEFORE_STOPLINE
                stop_wp = max(stop_wp, next_wp)  # next_wp for next_wp >= stop_wp
            else:
                # Keep on driving
                stop_wp = -1

        # Generate trajectory waypoint vector
        traj_waypoints = self.waypoints[next_wp:lookahead_wp]

        # Generate stop waypoint index in reference to trajectory vector
        traj_stop_wp = stop_wp - next_wp

        # Calculate distance from trajectory start waypoint (next waypoint) to current car position
        dist_next = self.distance(self.car_pose.position, traj_waypoints[0].pose.pose.position)

        # Generate distance vector for trajectory
        traj_distances = self.waypoint_distances[next_wp:lookahead_wp-1]
        traj_distances.insert(0, dist_next)

        if self.dbw_enabled:
            # Convert path to trajectory (plan ahead with constant acceleration/deceleration)
            traj_waypoints = self.path_to_trajectory(traj_waypoints, traj_distances, traj_stop_wp, self.force_update)
            self.force_update = False
        else:
            # Manual driving, set current velocity as planned velocity
            for i in range(len(traj_waypoints)):
                if self.linear_velocity is None:
                    current_velocity = 0.0
                else:
                    current_velocity = self.linear_velocity
                self.set_waypoint_velocity(traj_waypoints, i, current_velocity)

        # Generate Lane message to publish
        if traj_waypoints is not None:
            lane = Lane()
            lane.header.frame_id = '/trajectory'
            lane.header.stamp = rospy.Time(0)
            lane.waypoints = traj_waypoints

            # Update waypoints

            self.final_waypoints_pub.publish(lane)

        # **********************************************************
        # Debug output (csv and console)
        # **********************************************************
        if DEBUG_WAYPOINTS_CSV or DEBUG_WAYPOINTS_LOG:
            N_waypoints_debug = len(traj_waypoints)
            traj_waypoints_velx_debug = []
            traj_waypoints_posx_debug = []
            traj_waypoints_posy_debug = []
            for i_wp in range(N_waypoints_debug):
                traj_waypoints_velx_debug.append(traj_waypoints[i_wp].twist.twist.linear.x)
                traj_waypoints_posx_debug.append(traj_waypoints[i_wp].pose.pose.position.x)
                traj_waypoints_posy_debug.append(traj_waypoints[i_wp].pose.pose.position.y)

        if DEBUG_WAYPOINTS_CSV:
            # Declaration of self.csv_fields in __init__ method
            now = rospy.get_time()
            self.csv_data['time'] = now
            self.csv_data['x'] = self.car_pose.position.x
            self.csv_data['y'] = self.car_pose.position.y
            self.csv_data['v_target'] = self.velocity
            self.csv_data['v'] = self.linear_velocity
            self.csv_data['psi_p'] = self.angular_velocity
            self.csv_data['next_wp'] = next_wp
            self.csv_data['lookahead_wp'] = lookahead_wp
            self.csv_data['stop_wp'] = stop_wp
            self.csv_data['traj_stop_wp'] = traj_stop_wp
            self.csv_data['traj_waypoints_velx'] = traj_waypoints_velx_debug
            self.csv_data['traj_distances'] = traj_distances
            self.csv_data['traj_waypoints_posx'] = traj_waypoints_posx_debug
            self.csv_data['traj_waypoints_posy'] = traj_waypoints_posy_debug
            self.csv_writer.writerow(self.csv_data)

        if DEBUG_WAYPOINTS_LOG:
            print('***********************************************************')
            print("---> next_wp                 : {}".format(next_wp))
            print("---> lookahead_wp            : {}".format(lookahead_wp))
            print("---> stop_wp                 : {}".format(stop_wp))
            print("---> self.linear_velocity    : {}".format(self.linear_velocity))
            print("---> traj_waypoints_velx[0]  : {}".format(traj_waypoints_velx_debug[0]))
            print("---> traj_waypoints_velx[{}] : {}".format(len(traj_waypoints_velx_debug)-1, traj_waypoints_velx_debug[len(traj_waypoints_velx_debug)-1]))
            print("---> len(self.waypoints)     : {}".format(len(self.waypoints)))
            print('***********************************************************')

    # Helper function that generates a trajectory from the planned local waypoints
    # using given acceleration and deceleration values and taking into account the target speed
    def path_to_trajectory(self, waypoints, distances, stop_index=-1, force_update=False):

        # Number of path waypoints to associate a target velocity with (trajectory)
        num_waypoints = len(waypoints)

        # Select planning mode according to parameter
        if PLAN_ON_CURRENT_VELOCITY:
            # Start trajectory planning (positive accelerations) from current vehicle velocity
            current_velocity = self.linear_velocity
        else:
            # Start trajectory planning (positive accelerations) from corresponding waypoint velocity
            if force_update or abs(self.get_waypoint_velocity(waypoints[0])) == 0:
                # Plan relative to car-position
                current_velocity = self.linear_velocity
                # print("FORCE trajectory update v_cur = {}".format(current_velocity))
            else:
                # Continue last trajectory, plan relative to next waypoint
                current_velocity = self.get_waypoint_velocity(waypoints[0])
                distances[0] = 0.0
                # print("continue, trajectory, from  v = {}".format(current_velocity))

        if DEBUG_WAYPOINTS_LOG:
            print("---> initial_plan_velocity   : {}".format(current_velocity))

        if current_velocity is None:
            # try to stop if we do not know our current speed
            current_velocity = 0.0

        # Accelerate with given self.plan_acceleration to target velocity self.velocity
        if stop_index < 0:
            # Accelerate to target speed
            x_traj = 0
            for i in range(num_waypoints):
                x_traj += distances[i]
                v_traj = math.sqrt(current_velocity ** 2 + 2 * self.plan_acceleration * x_traj)
                # ensure minimum waypoint speed during acceleration to avoid deadlocks in standstill
                v_traj = max(v_traj, MIN_WAYPOINT_SPEED_ACC)
                self.set_waypoint_velocity(waypoints, i, min(self.velocity, v_traj))
        else:
            # Stop at stop-line with self.plan_deceleration
            dist_rem = sum(distances[0:stop_index])
            x_traj = 0
            for i in range(num_waypoints):
                x_traj += distances[i]
                # potential acceleration trajectory from current velocity till stopping initiated
                v_traj_acc = math.sqrt(current_velocity ** 2 + 2 * self.plan_acceleration * x_traj)
                # ensure minimum waypoint speed during acceleration to avoid deadlocks in standstill
                v_traj_acc = max(v_traj_acc, MIN_WAYPOINT_SPEED_ACC)
                if dist_rem > 0:
                    # needed stopping velocity trajectory with given plan_deceleration
                    v_traj = math.sqrt(2 * dist_rem * abs(self.plan_deceleration))
                else:
                    v_traj = 0.0

                # Limit trajectory velocity value by global waypoint velocity (target speed)
                v_traj = min(self.velocity, v_traj, v_traj_acc)

                # Set waypoint velocity values to generate trajectory as waypoints return value
                self.set_waypoint_velocity(waypoints, i, v_traj)
                # Decrease rest distance to stop-line
                delta = distances[i]
                dist_rem -= delta

        return waypoints


if __name__ == '__main__':
    try:
        WaypointUpdater()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start waypoint updater node.')
