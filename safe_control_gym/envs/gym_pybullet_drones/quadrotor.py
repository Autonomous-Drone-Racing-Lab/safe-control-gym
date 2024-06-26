"""Quadrotor environment using PyBullet physics.

Based on UTIAS Dynamic Systems Lab's gym-pybullet-drones:
    * https://github.com/utiasDSL/gym-pybullet-drones

"""

import os
import logging
import math
from copy import deepcopy
import casadi as cs
from gym import spaces
import numpy as np
import pybullet as p

from safe_control_gym.envs.benchmark_env import Cost, Task
from safe_control_gym.envs.constraints import GENERAL_CONSTRAINTS
from safe_control_gym.math_and_models.symbolic_systems import SymbolicModel
from safe_control_gym.envs.gym_pybullet_drones.base_aviary import BaseAviary
from safe_control_gym.envs.gym_pybullet_drones.quadrotor_utils import QuadType, cmd2pwm, pwm2rpm
from safe_control_gym.math_and_models.transformations import transform_trajectory, csRotXYZ
from munch import munchify

logger = logging.getLogger(__name__)


class Quadrotor(BaseAviary):
    """1D and 2D quadrotor environment task.

    Including symbolic model, constraints, randomization, adversarial disturbances,
    multiple cost functions, stabilization and trajectory tracking references.

    """

    NAME = "quadrotor"
    AVAILABLE_CONSTRAINTS = deepcopy(GENERAL_CONSTRAINTS)

    DISTURBANCE_MODES = {  # Set at runtime by QUAD_TYPE
        "observation": {"dim": -1},
        "action": {"dim": -1},
        "dynamics": {"dim": -1},
    }

    INERTIAL_PROP_RAND_INFO = {
        "M": {  # Nominal: 0.027
            "distrib": "uniform",
            "low": 0.022,
            "high": 0.032,
        },
        "Ixx": {  # Nominal: 1.4e-5
            "distrib": "uniform",
            "low": 1.3e-5,
            "high": 1.5e-5,
        },
        "Iyy": {  # Nominal: 1.4e-5
            "distrib": "uniform",
            "low": 1.3e-5,
            "high": 1.5e-5,
        },
        "Izz": {  # Nominal: 2.17e-5
            "distrib": "uniform",
            "low": 2.07e-5,
            "high": 2.27e-5,
        },
    }
    GATE_Z_LOW = 0.525
    GATE_Z_HIGH = 1.0
    OBSTACLE_Z = 1.05

    INIT_STATE_RAND_INFO = {
        "init_x": {"distrib": "uniform", "low": -0.5, "high": 0.5},
        "init_x_dot": {"distrib": "uniform", "low": -0.01, "high": 0.01},
        "init_y": {"distrib": "uniform", "low": -0.5, "high": 0.5},
        "init_y_dot": {"distrib": "uniform", "low": -0.01, "high": 0.01},
        "init_z": {"distrib": "uniform", "low": 0.1, "high": 1.5},
        "init_z_dot": {"distrib": "uniform", "low": -0.01, "high": 0.01},
        "init_phi": {"distrib": "uniform", "low": -0.3, "high": 0.3},
        "init_theta": {"distrib": "uniform", "low": -0.3, "high": 0.3},
        "init_psi": {"distrib": "uniform", "low": -0.3, "high": 0.3},
        "init_p": {"distrib": "uniform", "low": -0.01, "high": 0.01},
        "init_theta_dot": {  # Only used in 2D quad.
            "distrib": "uniform",
            "low": -0.01,
            "high": 0.01,
        },
        "init_q": {"distrib": "uniform", "low": -0.01, "high": 0.01},
        "init_r": {"distrib": "uniform", "low": -0.01, "high": 0.01},
    }

    TASK_INFO = {
        "stabilization_goal": [0, 1],
        "stabilization_goal_tolerance": 0.05,
        "trajectory_type": "circle",
        "num_cycles": 1,
        "trajectory_plane": "zx",
        "trajectory_position_offset": [0.5, 0],
        "trajectory_scale": -0.5,
        "proj_point": [0, 0, 0.5],
        "proj_normal": [0, 1, 1],
    }

    def __init__(
        self,
        init_state=None,
        inertial_prop=None,
        # custom args
        quad_type: QuadType = QuadType.TWO_D,
        norm_act_scale=0.1,
        obs_goal_horizon=0,
        rew_state_weight=1.0,
        rew_act_weight=0.0001,
        rew_exponential=True,
        done_on_out_of_bound=True,
        info_mse_metric_state_weight=None,
        **kwargs,
    ):
        """Initialize a quadrotor environment.

        Args:
            init_state (ndarray, optional): The initial state of the environment, (z, z_dot) or (x, x_dot, z, z_dot theta, theta_dot).
            inertial_prop (ndarray, optional): The inertial properties of the environment (mass, Iyy).
            quad_type (QuadType, optional): The choice of motion type (1D along z or 2D in the x-z plane).
            norm_act_scale (float): scaling the [-1,1] action space around hover thrust when `normalized_action_space` is True.
            obs_goal_horizon (int): how many future goal states to append to obervation.
            rew_state_weight (list/ndarray): quadratic weights for state in rl reward.
            rew_act_weight (list/ndarray): quadratic weights for action in rl reward.
            rew_exponential (bool): if to exponentiate negative quadratic cost to positive, bounded [0,1] reward.
            done_on_out_of_bound (bool): if to termiante when state is out of bound.
            info_mse_metric_state_weight (list/ndarray): quadratic weights for state in mse calculation for info dict.

        """
        # Select the 1D (moving along z) or 2D (moving in the xz plane) quadrotor.
        self.QUAD_TYPE = QuadType(quad_type)
        self.norm_act_scale = norm_act_scale
        self.obs_goal_horizon = obs_goal_horizon
        self.rew_state_weight = np.array(rew_state_weight, ndmin=1, dtype=float)
        self.rew_act_weight = np.array(rew_act_weight, ndmin=1, dtype=float)
        self.rew_exponential = rew_exponential
        self.done_on_out_of_bound = done_on_out_of_bound
        if info_mse_metric_state_weight is None:
            if self.QUAD_TYPE == QuadType.ONE_D:
                self.info_mse_metric_state_weight = np.array([1, 0], ndmin=1, dtype=float)
            elif self.QUAD_TYPE == QuadType.TWO_D:
                self.info_mse_metric_state_weight = np.array(
                    [1, 0, 1, 0, 0, 0], ndmin=1, dtype=float
                )
            elif self.QUAD_TYPE == QuadType.THREE_D:
                self.info_mse_metric_state_weight = np.array(
                    [1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0], ndmin=1, dtype=float
                )
            else:
                raise ValueError(f"Quad type {self.QUAD_TYPE} not implemented.")
        else:
            if (
                (self.QUAD_TYPE == QuadType.ONE_D and len(info_mse_metric_state_weight) == 2)
                or (self.QUAD_TYPE == QuadType.TWO_D and len(info_mse_metric_state_weight) == 6)
                or (self.QUAD_TYPE == QuadType.THREE_D and len(info_mse_metric_state_weight) == 12)
            ):
                self.info_mse_metric_state_weight = np.array(
                    info_mse_metric_state_weight, ndmin=1, dtype=float
                )
            else:
                raise ValueError("Wrong info_mse_metric_state_weight argument size.")

        # BaseAviary constructor, called after defining the custom args,
        # since some BenchmarkEnv init setup can be task(custom args)-dependent.
        super().__init__(init_state=init_state, inertial_prop=inertial_prop, **kwargs)

        # Store initial state info.
        self.INIT_STATE_LABELS = {
            QuadType.ONE_D: ["init_x", "init_x_dot"],
            QuadType.TWO_D: [
                "init_x",
                "init_x_dot",
                "init_z",
                "init_z_dot",
                "init_theta",
                "init_theta_dot",
            ],
            QuadType.THREE_D: [
                "init_x",
                "init_x_dot",
                "init_y",
                "init_y_dot",
                "init_z",
                "init_z_dot",
                "init_phi",
                "init_theta",
                "init_psi",
                "init_p",
                "init_q",
                "init_r",
            ],
        }
        if init_state is None:
            for init_name in self.INIT_STATE_RAND_INFO:  # Default zero state.
                self.__dict__[init_name.upper()] = 0.0
        else:
            if isinstance(init_state, np.ndarray):  # Full state as numpy array .
                for i, init_name in enumerate(self.INIT_STATE_LABELS[self.QUAD_TYPE]):
                    self.__dict__[init_name.upper()] = init_state[i]
            elif isinstance(init_state, dict):  # Partial state as dictionary.
                for init_name in self.INIT_STATE_LABELS[self.QUAD_TYPE]:
                    self.__dict__[init_name.upper()] = init_state.get(init_name, 0.0)
            else:
                raise ValueError("[ERROR] in Quadrotor.__init__(), init_state incorrect format.")

        # Remove randomization info of initial state components inconsistent with quad type.
        for init_name in list(self.INIT_STATE_RAND_INFO.keys()):
            if init_name not in self.INIT_STATE_LABELS[self.QUAD_TYPE]:
                self.INIT_STATE_RAND_INFO.pop(init_name, None)
        # Remove randomization info of inertial components inconsistent with quad type.
        if self.QUAD_TYPE == QuadType.ONE_D:
            # Do NOT randomize J for the 1D quadrotor.
            self.INERTIAL_PROP_RAND_INFO.pop("Ixx", None)
            self.INERTIAL_PROP_RAND_INFO.pop("Iyy", None)
            self.INERTIAL_PROP_RAND_INFO.pop("Izz", None)
        elif self.QUAD_TYPE == QuadType.TWO_D:
            # Only randomize Iyy for the 2D quadrotor.
            self.INERTIAL_PROP_RAND_INFO.pop("Ixx", None)
            self.INERTIAL_PROP_RAND_INFO.pop("Izz", None)

        # Override inertial properties of passed as arguments.
        if inertial_prop is None:
            pass
        elif self.QUAD_TYPE == QuadType.ONE_D and np.array(inertial_prop).shape == (1,):
            self.MASS = inertial_prop[0]
        elif self.QUAD_TYPE == QuadType.TWO_D and np.array(inertial_prop).shape == (2,):
            self.MASS, self.J[1, 1] = inertial_prop
        elif self.QUAD_TYPE == QuadType.THREE_D and np.array(inertial_prop).shape == (4,):
            self.MASS, self.J[0, 0], self.J[1, 1], self.J[2, 2] = inertial_prop
        elif isinstance(inertial_prop, dict):
            self.MASS = inertial_prop.get("M", self.MASS)
            self.J[0, 0] = inertial_prop.get("Ixx", self.J[0, 0])
            self.J[1, 1] = inertial_prop.get("Iyy", self.J[1, 1])
            self.J[2, 2] = inertial_prop.get("Izz", self.J[2, 2])
        else:
            raise ValueError("[ERROR] in Quadrotor.__init__(), inertial_prop incorrect format.")

        # Set prior/symbolic info.
        self._setup_symbolic()

        # Create X_GOAL and U_GOAL references for the assigned task.
        self.U_GOAL = np.ones(self.action_dim) * self.MASS * self.GRAVITY_ACC / self.action_dim
        if self.TASK == Task.STABILIZATION:
            if self.QUAD_TYPE == QuadType.ONE_D:
                self.X_GOAL = np.hstack(
                    [self.TASK_INFO["stabilization_goal"][1], 0.0]
                )  # x = {z, z_dot}.
            elif self.QUAD_TYPE == QuadType.TWO_D:
                self.X_GOAL = np.hstack(
                    [
                        self.TASK_INFO["stabilization_goal"][0],
                        0.0,
                        self.TASK_INFO["stabilization_goal"][1],
                        0.0,
                        0.0,
                        0.0,
                    ]
                )  # x = {x, x_dot, z, z_dot, theta, theta_dot}.
            elif self.QUAD_TYPE == QuadType.THREE_D:
                self.X_GOAL = np.hstack(
                    [
                        self.TASK_INFO["stabilization_goal"][0],
                        0.0,
                        self.TASK_INFO["stabilization_goal"][1],
                        0.0,
                        self.TASK_INFO["stabilization_goal"][2],
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                    ]
                )  # x = {x, x_dot, y, y_dot, z, z_dot, phi, theta, psi, p, q, r}.
        elif self.TASK == Task.TRAJ_TRACKING:
            POS_REF, VEL_REF, SPEED = self._generate_trajectory(
                traj_type=self.TASK_INFO["trajectory_type"],
                traj_length=self.EPISODE_LEN_SEC,
                num_cycles=self.TASK_INFO["num_cycles"],
                traj_plane=self.TASK_INFO["trajectory_plane"],
                position_offset=self.TASK_INFO["trajectory_position_offset"],
                scaling=self.TASK_INFO["trajectory_scale"],
                sample_time=self.CTRL_TIMESTEP,
            )  # Each of the 3 returned values is of shape (Ctrl timesteps, 3)
            if self.QUAD_TYPE == QuadType.ONE_D:
                self.X_GOAL = np.vstack(
                    [
                        POS_REF[:, 2],  # z
                        VEL_REF[:, 2],  # z_dot
                    ]
                ).transpose()
            elif self.QUAD_TYPE == QuadType.TWO_D:
                self.X_GOAL = np.vstack(
                    [
                        POS_REF[:, 0],  # x
                        VEL_REF[:, 0],  # x_dot
                        POS_REF[:, 2],  # z
                        VEL_REF[:, 2],  # z_dot
                        np.zeros(POS_REF.shape[0]),  # zeros
                        np.zeros(VEL_REF.shape[0]),
                    ]
                ).transpose()
            elif self.QUAD_TYPE == QuadType.THREE_D:
                # Additional transformation of the originally planar trajectory.
                POS_REF_TRANS, VEL_REF_TRANS = transform_trajectory(
                    POS_REF,
                    VEL_REF,
                    trans_info={
                        "point": self.TASK_INFO["proj_point"],
                        "normal": self.TASK_INFO["proj_normal"],
                    },
                )
                self.X_GOAL = np.vstack(
                    [
                        POS_REF_TRANS[:, 0],  # x
                        VEL_REF_TRANS[:, 0],  # x_dot
                        POS_REF_TRANS[:, 1],  # y
                        VEL_REF_TRANS[:, 1],  # y_dot
                        POS_REF_TRANS[:, 2],  # z
                        VEL_REF_TRANS[:, 2],  # z_dot
                        np.zeros(POS_REF_TRANS.shape[0]),  # zeros
                        np.zeros(POS_REF_TRANS.shape[0]),
                        np.zeros(POS_REF_TRANS.shape[0]),
                        np.zeros(VEL_REF_TRANS.shape[0]),
                        np.zeros(VEL_REF_TRANS.shape[0]),
                        np.zeros(VEL_REF_TRANS.shape[0]),
                    ]
                ).transpose()

        # Equilibrium point at hover for linearization.
        self.X_EQ = np.zeros(self.state_dim)
        self.U_EQ = self.U_GOAL

        # IROS 2022 - Load maze.
        self.OBSTACLES = []
        self.GATES = []
        if "obstacles" in kwargs:
            self.OBSTACLES = kwargs["obstacles"]
        self.OBSTACLES = np.array(self.OBSTACLES)
        if "gates" in kwargs:
            self.GATES = kwargs["gates"]
        self.GATES = np.array(self.GATES)
        self.NUM_GATES = len(self.GATES)
        self.n_obstacles = len(self.OBSTACLES)
        if kwargs.get("randomized_gates_and_obstacles", False):
            self.RANDOMIZED_GATES_AND_OBS = True
            if "gates_and_obstacles_randomization_info" not in kwargs:
                raise ValueError(
                    "Missing 'gates_and_obstacles_randomization_info' in YAML configuration."
                )
            self.GATES_AND_OBS_RAND_INFO = kwargs["gates_and_obstacles_randomization_info"]
        else:
            self.RANDOMIZED_GATES_AND_OBS = False
        #
        self.DONE_ON_COLLISION = kwargs.get("done_on_collision", False)
        self.DONE_ON_COMPLETION = kwargs.get("done_on_completion", False)



        # save_base_path = "/home/tim/code/rl_experiments/lsy_drone_racing_rl/logs/gate_pos"
        # # iterate over path, and add next possible file {gate_pos_0.txt, gate_pos_1.txt, ...}
        # base_name = "gate_pos_{:d}.txt"
        # i = 0
        # while os.path.exists(os.path.join(save_base_path, base_name.format(i))):
        #     i += 1
        # self.save_path = os.path.join(save_base_path, base_name.format(i))



    def reset(self, **kwargs):
        """(Re-)initializes the environment to start an episode.

        Mandatory to call at least once after __init__().

        Args:
            kwargs: Additional arguments to pass to the reset method. Ooptional arguments are
            'initial_target_gate_id' (int): first gate to pass through in this episode, initializing point will be set automatically, either within prev gate, or intial point

        Returns:
            ndarray: The initial state of the environment.
            dict: A dictionary with information about the dynamics and constraints symbolic models.

        """
        super().before_reset()
        # PyBullet simulation reset.
        super()._reset_simulation()

        # IROS 2022 - Create maze.
        self.OBSTACLES_IDS = []
        self.obstacle_poses = []
        if self.RANDOMIZED_GATES_AND_OBS:
            rand_info_copy = deepcopy(self.GATES_AND_OBS_RAND_INFO)
            distrib = getattr(self.np_random, rand_info_copy["obstacles"].pop("distrib"))
            d_args = rand_info_copy["obstacles"].pop("args", [])
            d_kwargs = rand_info_copy["obstacles"]
        for obstacle in self.OBSTACLES:
            obs_height = 0.525  # URDF dependent, places 'obstacle.urdf' at z == 0.
            if self.RANDOMIZED_GATES_AND_OBS:
                offset = np.array(
                    [distrib(*d_args, **d_kwargs), distrib(*d_args, **d_kwargs), obs_height]
                )
                pose_disturbance = np.array([0, 0, distrib(*d_args, **d_kwargs)])
            else:
                offset = np.array([0, 0, obs_height])
                pose_disturbance = np.array([0, 0, 0])
            self.obstacle_poses.append(
                list(np.array(obstacle[0:3]) + offset)
                + list(np.array(obstacle[3:6]) + pose_disturbance)
            )
            TMP_ID = p.loadURDF(
                os.path.join(self.URDF_DIR, "obstacle.urdf"),
                np.array(obstacle[0:3]) + offset,
                p.getQuaternionFromEuler(np.array(obstacle[3:6]) + pose_disturbance),
                physicsClientId=self.PYB_CLIENT,
            )
            p.addUserDebugText(
                str(TMP_ID),
                textPosition=[0, 0, 0.5],
                textColorRGB=[1, 0, 0],
                lifeTime=self.EPISODE_LEN_SEC,
                textSize=1.5,
                parentObjectUniqueId=TMP_ID,
                parentLinkIndex=-1,
                physicsClientId=self.PYB_CLIENT,
            )
            self.OBSTACLES_IDS.append(TMP_ID)
        self.obstacle_poses = np.array(self.obstacle_poses)
        self.obstacle_poses[:, 2] = self.OBSTACLE_Z
        self.GATES_IDS = []
        self._gates_pose = []
        if self.RANDOMIZED_GATES_AND_OBS:
            rand_info_copy = deepcopy(self.GATES_AND_OBS_RAND_INFO)
            distrib = getattr(self.np_random, rand_info_copy["gates"].pop("distrib"))
            d_args = rand_info_copy["gates"].pop("args", [])
            d_kwargs = rand_info_copy["gates"]
        for gate in self.GATES:
            if gate[6] == 0:
                urdf_file = "portal.urdf"
                gate_height = self.GATE_Z_HIGH  # URDF dependent, places 'portal.urdf' at z == 0.
            elif gate[6] == 1:
                urdf_file = "low_portal.urdf"
                gate_height = self.GATE_Z_LOW  # URDF dependent, places 'low_portal.urdf' at z == 0.
            else:
                raise ValueError("[ERROR] Unknown gate type.")
            if self.RANDOMIZED_GATES_AND_OBS:
                offset = np.array(
                    [distrib(*d_args, **d_kwargs), distrib(*d_args, **d_kwargs), gate_height]
                )
                pose_disturbance = np.array([0, 0, distrib(*d_args, **d_kwargs)])
            else:
                offset = np.array([0, 0, gate_height])
                pose_disturbance = np.array([0, 0, 0])
            self._gates_pose.append(
                list(np.array(gate[0:3]) + offset) + list(np.array(gate[3:6]) + pose_disturbance)
            )
            TMP_ID = p.loadURDF(
                os.path.join(self.URDF_DIR, urdf_file),
                np.array(gate[0:3]) + offset,
                p.getQuaternionFromEuler(np.array(gate[3:6]) + pose_disturbance),
                physicsClientId=self.PYB_CLIENT,
            )
            p.addUserDebugText(
                str(TMP_ID),
                textPosition=[0, 0, 0.5],
                textColorRGB=[1, 0, 0],
                lifeTime=self.EPISODE_LEN_SEC,
                textSize=1.5,
                parentObjectUniqueId=TMP_ID,
                parentLinkIndex=-1,
                physicsClientId=self.PYB_CLIENT,
            )
            self.GATES_IDS.append(TMP_ID)
        self._gates_pose = np.array(self._gates_pose)
        
        if kwargs.get("initial_target_gate_id", None) is not None:
            #logger.info(f"Resett provided custom gate start id: {kwargs['initial_target_gate_id']}")
            self.current_gate = kwargs["initial_target_gate_id"]
        else:
            self.current_gate = 0
        #
        # Deactivate select collisions, e.g. between the ground plane and the drone
        # p.setCollisionFilterPair(bodyUniqueIdA=self.PLANE_ID,
        #                          bodyUniqueIdB=self.DRONE_IDS[0],
        #                          linkIndexA=-1,
        #                          linkIndexB=-1,
        #                          enableCollision=0,
        #                          physicsClientId=self.PYB_CLIENT)
        #
        # Initialize IROS-specific attributes.
        self.stepped_through_gate = False
        self.currently_collided = False
        self.at_goal_pos = False
        self.steps_at_goal_pos = 0
        self.task_completed = False

        # Choose randomized or deterministic inertial properties.
        prop_values = {
            "M": self.MASS,
            "Ixx": self.J[0, 0],
            "Iyy": self.J[1, 1],
            "Izz": self.J[2, 2],
        }
        if self.RANDOMIZED_INERTIAL_PROP:
            prop_values = self._randomize_values_by_info(prop_values, self.INERTIAL_PROP_RAND_INFO)
            if any(phy_quantity < 0 for phy_quantity in prop_values.values()):
                raise ValueError(
                    "[ERROR] in Quadrotor.reset(), negative randomized inertial properties."
                )
        self.OVERRIDDEN_QUAD_MASS = prop_values["M"]
        self.OVERRIDDEN_QUAD_INERTIA = [prop_values["Ixx"], prop_values["Iyy"], prop_values["Izz"]]

        # Override inertial properties.
        p.changeDynamics(
            self.DRONE_IDS[0],
            linkIndex=-1,  # Base link.
            mass=self.OVERRIDDEN_QUAD_MASS,
            localInertiaDiagonal=self.OVERRIDDEN_QUAD_INERTIA,
            physicsClientId=self.PYB_CLIENT,
        )

        # Randomize initial state.
        if (init_gate_id:=kwargs.get("initial_target_gate_id", None)) is not None and init_gate_id != 0: # init gate = 0, then we use original start position
            #assert self.QUAD_TYPE == QuadType.THREE_D, "Custom initial state only for 3D quad."
            # logger.info(f"Resert provided custom start goal, starting from gate: {init_gate_id}")
            assert init_gate_id < self.NUM_GATES, "Custom initial gate id out of range."
            
            ref_gate_pose = self._gates_pose[init_gate_id - 1] # sample prev gate so that we can then fly through current
            INIT_XYZ = ref_gate_pose[[0, 1, 2]].tolist()
            INIT_VEL = [0, 0, 0]
            INIT_RPY = ref_gate_pose[[3, 4, 5]].tolist()
            INIT_ANG_VEL = [0, 0, 0]
        
        else:
            init_values = {
                init_name: self.__dict__[init_name.upper()]
                for init_name in self.INIT_STATE_LABELS[self.QUAD_TYPE]
            }
            if self.RANDOMIZED_INIT:
                init_values = self._randomize_values_by_info(init_values, self.INIT_STATE_RAND_INFO)
            INIT_XYZ = [init_values.get("init_" + k, 0.0) for k in ["x", "y", "z"]]
            INIT_VEL = [init_values.get("init_" + k + "_dot", 0.0) for k in ["x", "y", "z"]]
            INIT_RPY = [init_values.get("init_" + k, 0.0) for k in ["phi", "theta", "psi"]]
            if self.QUAD_TYPE == QuadType.TWO_D:
                INIT_ANG_VEL = [0, init_values.get("init_theta_dot", 0.0), 0]
            else:
                INIT_ANG_VEL = [init_values.get("init_" + k, 0.0) for k in ["p", "q", "r"]]
        p.resetBasePositionAndOrientation(
            self.DRONE_IDS[0],
            INIT_XYZ,
            p.getQuaternionFromEuler(INIT_RPY),
            physicsClientId=self.PYB_CLIENT,
        )
        p.resetBaseVelocity(
            self.DRONE_IDS[0], INIT_VEL, INIT_ANG_VEL, physicsClientId=self.PYB_CLIENT
        )

        # Update BaseAviary internal variables before calling self._get_observation().
        self._update_and_store_kinematic_information()
        obs, info = self._get_observation(), self._get_reset_info()
        obs, info = super().after_reset(obs, info)

        
        # with open(self.save_path, "a") as f:
        #     # write gates as list 
        #     for gate in self._gates_pose:
        #         str_gate = f"[{gate[0]:.4f}, {gate[1]:.4f}, {gate[2]:.4f}, {gate[3]:.4f}, {gate[4]:.4f}, {gate[5]:.4f}]"
        #         f.write(str_gate)
        #     f.write("\n")

        # Return either an observation and dictionary or just the observation.
        if self.INFO_IN_RESET:
            return obs, info
        else:
            return obs

    def step(self, action):
        """Advances the environment by one control step.

        Pass the commanded RPMs and the adversarial force to the superclass .step().
        The PyBullet simulation is stepped PYB_FREQ/CTRL_FREQ times in BaseAviary.

        Args:
            action (ndarray): the action applied to the environment for the step.

        Returns:
            ndarray: The state of the environment after the step.
            float: The scalar reward/cost of the step.
            bool: Whether the conditions for the end of an episode are met in the step.
            dict: A dictionary with information about the constraints evaluations and violations.

        """
        # Get the preprocessed rpm for each motor
        rpm = super().before_step(action)

        # Determine disturbance force.
        disturb_force = None
        passive_disturb = "dynamics" in self.disturbances
        adv_disturb = self.adversary_disturbance == "dynamics"
        if passive_disturb or adv_disturb:
            disturb_force = np.zeros(self.DISTURBANCE_MODES["dynamics"]["dim"])
        if passive_disturb:
            disturb_force = self.disturbances["dynamics"].apply(disturb_force, self)
        if adv_disturb and self.adv_action is not None:
            disturb_force = disturb_force + self.adv_action
            # Clear the adversary action, wait for the next one.
            self.adv_action = None
        # Construct full (3D) disturbance force.
        if disturb_force is not None:
            if self.QUAD_TYPE == QuadType.ONE_D:
                # Only disturb on z direction.
                disturb_force = [0, 0, float(disturb_force)]
            elif self.QUAD_TYPE == QuadType.TWO_D:
                # Only disturb on x-z plane.
                disturb_force = [float(disturb_force[0]), 0, float(disturb_force[1])]
            elif self.QUAD_TYPE == QuadType.THREE_D:
                disturb_force = np.asarray(disturb_force).flatten()

        # Advance the simulation.
        super()._advance_simulation(rpm, disturb_force)
        # Standard Gym return.
        obs = self._get_observation()
        info = self._get_info()
        # IROS 2022 - After _get_info() to use this step's 'self' attributes.
        done = self._get_done()
        rew = self._get_reward()
        obs, rew, done, info = super().after_step(obs, rew, done, info)
        return obs, rew, done, info

    def render(self, mode="human"):
        """Retrieves a frame from PyBullet rendering.

        Args:
            mode (str): Unused.

        Returns:
            ndarray: A multidimensional array with the RGB frame captured by PyBullet's camera.

        """
        [w, h, rgb, dep, seg] = p.getCameraImage(
            width=self.RENDER_WIDTH,
            height=self.RENDER_HEIGHT,
            shadow=1,
            viewMatrix=self.CAM_VIEW,
            projectionMatrix=self.CAM_PRO,
            renderer=p.ER_TINY_RENDERER,
            flags=p.ER_SEGMENTATION_MASK_OBJECT_AND_LINKINDEX,
            physicsClientId=self.PYB_CLIENT,
        )
        # Image.fromarray(np.reshape(rgb, (h, w, 4)), 'RGBA').show()
        return np.reshape(rgb, (h, w, 4))

    def _setup_symbolic(self):
        """Creates symbolic (CasADi) models for dynamics, observation, and cost.

        Returns:
            SymbolicModel: CasADi symbolic model of the environment.

        """
        m, g, l = self.MASS, self.GRAVITY_ACC, self.L
        Iyy = self.J[1, 1]
        dt = self.CTRL_TIMESTEP
        # Define states.
        z = cs.MX.sym("z")
        z_dot = cs.MX.sym("z_dot")
        if self.QUAD_TYPE == QuadType.ONE_D:
            nx, nu = 2, 1
            # Define states.
            X = cs.vertcat(z, z_dot)
            # Define input thrust.
            T = cs.MX.sym("T")
            U = cs.vertcat(T)
            # Define dynamics equations.
            X_dot = cs.vertcat(z_dot, T / m - g)
            # Define observation equation.
            Y = cs.vertcat(z, z_dot)
        elif self.QUAD_TYPE == QuadType.TWO_D:
            nx, nu = 6, 2
            # Define states.
            x = cs.MX.sym("x")
            x_dot = cs.MX.sym("x_dot")
            theta = cs.MX.sym("theta")
            theta_dot = cs.MX.sym("theta_dot")
            X = cs.vertcat(x, x_dot, z, z_dot, theta, theta_dot)
            # Define input thrusts.
            T1 = cs.MX.sym("T1")
            T2 = cs.MX.sym("T2")
            U = cs.vertcat(T1, T2)
            # Define dynamics equations.
            X_dot = cs.vertcat(
                x_dot,
                cs.sin(theta) * (T1 + T2) / m,
                z_dot,
                cs.cos(theta) * (T1 + T2) / m - g,
                theta_dot,
                l * (T2 - T1) / Iyy / np.sqrt(2),
            )
            # Define observation.
            Y = cs.vertcat(x, x_dot, z, z_dot, theta, theta_dot)
        elif self.QUAD_TYPE == QuadType.THREE_D:
            nx, nu = 12, 4
            Ixx = self.J[0, 0]
            Izz = self.J[2, 2]
            J = cs.blockcat([[Ixx, 0.0, 0.0], [0.0, Iyy, 0.0], [0.0, 0.0, Izz]])
            Jinv = cs.blockcat(
                [[1.0 / Ixx, 0.0, 0.0], [0.0, 1.0 / Iyy, 0.0], [0.0, 0.0, 1.0 / Izz]]
            )
            gamma = self.KM / self.KF
            x = cs.MX.sym("x")
            y = cs.MX.sym("y")
            phi = cs.MX.sym("phi")  # Roll
            theta = cs.MX.sym("theta")  # Pitch
            psi = cs.MX.sym("psi")  # Yaw
            x_dot = cs.MX.sym("x_dot")
            y_dot = cs.MX.sym("y_dot")
            p = cs.MX.sym("p")  # Body frame roll rate
            q = cs.MX.sym("q")  # body frame pith rate
            r = cs.MX.sym("r")  # body frame yaw rate
            # PyBullet Euler angles use the SDFormat for rotation matrices.
            Rob = csRotXYZ(
                phi, theta, psi
            )  # rotation matrix transforming a vector in the body frame to the world frame.

            # Define state variables.
            X = cs.vertcat(x, x_dot, y, y_dot, z, z_dot, phi, theta, psi, p, q, r)

            # Define inputs.
            f1 = cs.MX.sym("f1")
            f2 = cs.MX.sym("f2")
            f3 = cs.MX.sym("f3")
            f4 = cs.MX.sym("f4")
            U = cs.vertcat(f1, f2, f3, f4)

            # From Ch. 2 of Luis, Carlos, and Jérôme Le Ny. "Design of a trajectory tracking
            # controller for a nanoquadcopter." arXiv preprint arXiv:1608.05786 (2016).

            # Defining the dynamics function.
            # We are using the velocity of the base wrt to the world frame expressed in the world
            # frame. Note that the reference expresses this in the body frame.
            oVdot_cg_o = Rob @ cs.vertcat(0, 0, f1 + f2 + f3 + f4) / m - cs.vertcat(0, 0, g)
            pos_ddot = oVdot_cg_o
            pos_dot = cs.vertcat(x_dot, y_dot, z_dot)
            Mb = cs.vertcat(
                l / cs.sqrt(2.0) * (f1 + f2 - f3 - f4),
                l / cs.sqrt(2.0) * (-f1 + f2 + f3 - f4),
                gamma * (f1 - f2 + f3 - f4),
            )
            rate_dot = Jinv @ (Mb - (cs.skew(cs.vertcat(p, q, r)) @ J @ cs.vertcat(p, q, r)))
            ang_dot = cs.blockcat(
                [
                    [1, cs.sin(phi) * cs.tan(theta), cs.cos(phi) * cs.tan(theta)],
                    [0, cs.cos(phi), -cs.sin(phi)],
                    [0, cs.sin(phi) / cs.cos(theta), cs.cos(phi) / cs.cos(theta)],
                ]
            ) @ cs.vertcat(p, q, r)
            X_dot = cs.vertcat(
                pos_dot[0],
                pos_ddot[0],
                pos_dot[1],
                pos_ddot[1],
                pos_dot[2],
                pos_ddot[2],
                ang_dot,
                rate_dot,
            )

            Y = cs.vertcat(x, x_dot, y, y_dot, z, z_dot, phi, theta, psi, p, q, r)
        # Define cost (quadratic form).
        Q = cs.MX.sym("Q", nx, nx)
        R = cs.MX.sym("R", nu, nu)
        Xr = cs.MX.sym("Xr", nx, 1)
        Ur = cs.MX.sym("Ur", nu, 1)
        cost_func = 0.5 * (X - Xr).T @ Q @ (X - Xr) + 0.5 * (U - Ur).T @ R @ (U - Ur)
        # Define dynamics and cost dictionaries.
        dynamics = {"dyn_eqn": X_dot, "obs_eqn": Y, "vars": {"X": X, "U": U}}
        cost = {
            "cost_func": cost_func,
            "vars": {"X": X, "U": U, "Xr": Xr, "Ur": Ur, "Q": Q, "R": R},
        }
        # Setup symbolic model.
        self.symbolic = SymbolicModel(dynamics=dynamics, cost=cost, dt=dt)

    def _set_action_space(self):
        """Returns the action space of the environment.

        Returns:
            gym.spaces: The quadrotor environment's action space, of size 1 or 2 depending on
                QUAD_TYPE.

        """
        # Define action/input dimension, labels, and units.
        if self.QUAD_TYPE == QuadType.ONE_D:
            action_dim = 1
            self.ACTION_LABELS = ["T"]
            self.ACTION_UNITS = ["N"] if not self.NORMALIZED_RL_ACTION_SPACE else ["-"]
        elif self.QUAD_TYPE == QuadType.TWO_D:
            action_dim = 2
            self.ACTION_LABELS = ["T1", "T2"]
            self.ACTION_UNITS = ["N", "N"] if not self.NORMALIZED_RL_ACTION_SPACE else ["-", "-"]
        elif self.QUAD_TYPE == QuadType.THREE_D:
            action_dim = 4
            self.ACTION_LABELS = ["T1", "T2", "T3", "T4"]
            self.ACTION_UNITS = (
                ["N", "N", "N", "N"]
                if not self.NORMALIZED_RL_ACTION_SPACE
                else ["-", "-", "-", "-"]
            )

        if self.NORMALIZED_RL_ACTION_SPACE:
            # Normalized thrust (around hover thrust).
            self.hover_thrust = self.GRAVITY_ACC * self.MASS / action_dim
            self.action_space = spaces.Box(
                low=-np.ones(action_dim), high=np.ones(action_dim), dtype=np.float32
            )
        else:
            # Direct thrust control.
            n_motors = 4 / action_dim
            a_low = (
                self.KF * n_motors * (self.PWM2RPM_SCALE * self.MIN_PWM + self.PWM2RPM_CONST) ** 2
            )
            a_high = (
                self.KF * n_motors * (self.PWM2RPM_SCALE * self.MAX_PWM + self.PWM2RPM_CONST) ** 2
            )
            self.action_space = spaces.Box(
                low=np.full(action_dim, a_low, np.float32),
                high=np.full(action_dim, a_high, np.float32),
                dtype=np.float32,
            )

    def _set_observation_space(self):
        """Returns the observation space of the environment.

        Returns:
            gym.spaces: The bounded observation (state) space, of size 2, 6, or 12 depending on
                QUAD_TYPE.

        """
        self.x_threshold = 5
        self.y_threshold = 5
        self.z_threshold = 2.5
        self.phi_threshold_radians = 85 * math.pi / 180
        self.theta_threshold_radians = 85 * math.pi / 180
        self.psi_threshold_radians = 180 * math.pi / 180  # Do not bound yaw.

        # Define obs/state bounds, labels and units.
        if self.QUAD_TYPE == QuadType.ONE_D:
            # obs/state = {z, z_dot}.
            low = np.array([self.GROUND_PLANE_Z, -np.finfo(np.float32).max])
            high = np.array([self.z_threshold, np.finfo(np.float32).max])
            self.STATE_LABELS = ["z", "z_dot"]
            self.STATE_UNITS = ["m", "m/s"]
        elif self.QUAD_TYPE == QuadType.TWO_D:
            # obs/state = {x, x_dot, z, z_dot, theta, theta_dot}.
            low = np.array(
                [
                    -self.x_threshold,
                    -np.finfo(np.float32).max,
                    self.GROUND_PLANE_Z,
                    -np.finfo(np.float32).max,
                    -self.theta_threshold_radians,
                    -np.finfo(np.float32).max,
                ]
            )
            high = np.array(
                [
                    self.x_threshold,
                    np.finfo(np.float32).max,
                    self.z_threshold,
                    np.finfo(np.float32).max,
                    self.theta_threshold_radians,
                    np.finfo(np.float32).max,
                ]
            )
            self.STATE_LABELS = ["x", "x_dot", "z", "z_dot", "theta", "theta_dot"]
            self.STATE_UNITS = ["m", "m/s", "m", "m/s", "rad", "rad/s"]
        elif self.QUAD_TYPE == QuadType.THREE_D:
            # obs/state = {x, x_dot, y, y_dot, z, z_dot, phi, theta, psi, p, q, r}.
            low = np.array(
                [
                    -self.x_threshold,
                    -np.finfo(np.float32).max,
                    -self.y_threshold,
                    -np.finfo(np.float32).max,
                    self.GROUND_PLANE_Z,
                    -np.finfo(np.float32).max,
                    -self.phi_threshold_radians,
                    -self.theta_threshold_radians,
                    -self.psi_threshold_radians,
                    -np.finfo(np.float32).max,
                    -np.finfo(np.float32).max,
                    -np.finfo(np.float32).max,
                ]
            )
            high = np.array(
                [
                    self.x_threshold,
                    np.finfo(np.float32).max,
                    self.y_threshold,
                    np.finfo(np.float32).max,
                    self.z_threshold,
                    np.finfo(np.float32).max,
                    self.phi_threshold_radians,
                    self.theta_threshold_radians,
                    self.psi_threshold_radians,
                    np.finfo(np.float32).max,
                    np.finfo(np.float32).max,
                    np.finfo(np.float32).max,
                ]
            )
            self.STATE_LABELS = [
                "x",
                "x_dot",
                "y",
                "y_dot",
                "z",
                "z_dot",
                "phi",
                "theta",
                "psi",
                "p",
                "q",
                "r",
            ]
            self.STATE_UNITS = [
                "m",
                "m/s",
                "m",
                "m/s",
                "m",
                "m/s",
                "rad",
                "rad",
                "rad",
                "rad/s",
                "rad/s",
                "rad/s",
            ]
        # Define the state space for the dynamics.
        self.state_space = spaces.Box(low=low, high=high, dtype=np.float32)

        # Concatenate reference for RL.
        if (
            self.COST == Cost.RL_REWARD
            and self.TASK == Task.TRAJ_TRACKING
            and self.obs_goal_horizon > 0
        ):
            # Include future goal state(s).
            # e.g. horizon=1, obs = {state, state_target}
            mul = 1 + self.obs_goal_horizon
            low = np.concatenate([low] * mul)
            high = np.concatenate([high] * mul)
        elif (
            self.COST == Cost.RL_REWARD
            and self.TASK == Task.STABILIZATION
            and self.obs_goal_horizon > 0
        ):
            low = np.concatenate([low] * 2)
            high = np.concatenate([high] * 2)

        # Define obs space exposed to the controller.
        # Note how the obs space can differ from state space (i.e. augmented with the next reference states for RL)
        self.observation_space = spaces.Box(low=low, high=high, dtype=np.float32)

    def _setup_disturbances(self):
        # Custom disturbance info.
        self.DISTURBANCE_MODES["observation"]["dim"] = self.obs_dim
        self.DISTURBANCE_MODES["action"]["dim"] = self.action_dim
        self.DISTURBANCE_MODES["dynamics"]["dim"] = int(self.QUAD_TYPE)
        super()._setup_disturbances()

    def _preprocess_control(self, action):
        """Converts the action passed to .step() into motors' RPMs (ndarray of shape (4,)).

        Args:
            action (ndarray): The raw action input, of size 1 or 2 depending on QUAD_TYPE.

        Returns:
            ndarray: The motors RPMs to apply to the quadrotor.

        """
        if self.NORMALIZED_RL_ACTION_SPACE:
            # rescale action to around hover thrust
            action = np.clip(action, self.action_space.low, self.action_space.high)
            thrust = (1 + self.norm_act_scale * action) * self.hover_thrust
        else:
            thrust = np.clip(action, self.action_space.low, self.action_space.high)
        if not np.array_equal(thrust, np.array(action)) and self.VERBOSE:
            logger.warning("Action was clipped in Quadrotor._preprocess_control().")
        self.current_preprocessed_action = thrust
        # Apply disturbances.
        if "action" in self.disturbances:
            thrust = self.disturbances["action"].apply(thrust, self)
        if self.adversary_disturbance == "action":
            thrust = thrust + self.adv_action
        # convert to quad motor rpm commands
        pwm = cmd2pwm(
            thrust, self.PWM2RPM_SCALE, self.PWM2RPM_CONST, self.KF, self.MIN_PWM, self.MAX_PWM
        )
        rpm = pwm2rpm(pwm, self.PWM2RPM_SCALE, self.PWM2RPM_CONST)
        return rpm

    def _get_observation(self):
        """Returns the current observation (state) of the environment.

        Returns:
            ndarray: The state of the quadrotor, of size 2 or 6 depending on QUAD_TYPE.

        """
        full_state = self._get_drone_state_vector(0)
        pos, _, rpy, vel, ang_v, _ = np.split(full_state, [3, 7, 10, 13, 16])
        if self.QUAD_TYPE == QuadType.ONE_D:
            # {z, z_dot}.
            self.state = np.hstack([pos[2], vel[2]]).reshape((2,))
        elif self.QUAD_TYPE == QuadType.TWO_D:
            # {x, x_dot, z, z_dot, theta, theta_dot}.
            self.state = np.hstack([pos[0], vel[0], pos[2], vel[2], rpy[1], ang_v[1]]).reshape((6,))
        elif self.QUAD_TYPE == QuadType.THREE_D:
            Rob = np.array(p.getMatrixFromQuaternion(self.quat[0])).reshape((3, 3))
            Rbo = Rob.T
            ang_v_body_frame = Rbo @ ang_v
            # {x, x_dot, y, y_dot, z, z_dot, phi, theta, psi, p, q, r}.
            self.state = np.hstack(
                [pos[0], vel[0], pos[1], vel[1], pos[2], vel[2], rpy, ang_v_body_frame]
            ).reshape((12,))
        # Apply observation disturbance.
        obs = deepcopy(self.state)
        if "observation" in self.disturbances:
            obs = self.disturbances["observation"].apply(obs, self)

        # Concatenate goal info (references state(s)) for RL.
        obs = self.extend_obs(obs, self.ctrl_step_counter + 1)
        return obs

    def _get_reward(self):
        """Computes the current step's reward value.

        Returns:
            float: The evaluated reward/cost.

        """
        # RL cost.
        if self.COST == Cost.RL_REWARD:
            state = self.state
            act = np.asarray(self.current_preprocessed_action)
            act_error = act - self.U_GOAL
            # Quadratic costs w.r.t state and action
            # TODO: consider using multiple future goal states for cost in tracking
            if self.TASK == Task.STABILIZATION:
                state_error = state - self.X_GOAL
                dist = np.sum(self.rew_state_weight * state_error * state_error)
                dist += np.sum(self.rew_act_weight * act_error * act_error)
            if self.TASK == Task.TRAJ_TRACKING:
                wp_idx = min(self.ctrl_step_counter, self.X_GOAL.shape[0] - 1)
                state_error = state - self.X_GOAL[wp_idx]
                dist = np.sum(self.rew_state_weight * state_error * state_error)
                dist += np.sum(self.rew_act_weight * act_error * act_error)
            rew = -dist
            # Convert rew to be positive and bounded [0,1].
            if self.rew_exponential:
                rew = np.exp(rew)
            return rew

        # Control cost.
        if self.COST == Cost.QUADRATIC:
            if self.TASK == Task.STABILIZATION:
                return float(
                    -1
                    * self.symbolic.loss(
                        x=self.state,
                        Xr=self.X_GOAL,
                        u=self.current_preprocessed_action,
                        Ur=self.U_GOAL,
                        Q=self.Q,
                        R=self.R,
                    )["l"]
                )
            if self.TASK == Task.TRAJ_TRACKING:
                return float(
                    -1
                    * self.symbolic.loss(
                        x=self.state,
                        Xr=self.X_GOAL[self.ctrl_step_counter, :],
                        u=self.current_preprocessed_action,
                        Ur=self.U_GOAL,
                        Q=self.Q,
                        R=self.R,
                    )["l"]
                )

        # IROS 2022 - Competition sparse reward signal.
        if self.COST == Cost.COMPETITION:
            reward = 0
            # Reward for stepping through the (correct) next gate.
            if self.stepped_through_gate:
                reward += 100
            # Reward for reaching goal position (after navigating the gates in the correct order).
            if self.at_goal_pos:
                reward += 100
            # Penalize by collision.
            if self.currently_collided:
                reward -= 1000
            # Penalize by constraint violation.
            if self.cnstr_violation:
                reward -= 100
            # Penalize by loss from X_GOAL, U_GOAL state.
            # reward += float(-1 * self.symbolic.loss(x=self.state,
            #                                         Xr=self.X_GOAL,
            #                                         u=self.current_preprocessed_action,
            #                                         Ur=self.U_GOAL,
            #                                         Q=self.Q,
            #                                         R=self.R)["l"])
            return reward

    def _get_done(self):
        """Computes the conditions for termination of an episode.

        Returns:
            bool: Whether an episode is over.

        """
        # Done if goal reached for stabilization task with quadratic cost.

        #! Removed prev code as it collides with later calculation of task_completed

        # # Done if the episode length is exceeded.
        # if (self.ctrl_step_counter + 1) / self.CTRL_FREQ >= self.EPISODE_LEN_SEC:
        #     return True

        # # Done if a constraint is violated.
        # if self.constraints is not None:
        #     if self.DONE_ON_VIOLATION and self.constraints.is_violated(self):
        #         return True

        # Done if state is out-of-bounds.
        if self.done_on_out_of_bound:
            if self.QUAD_TYPE == QuadType.ONE_D:
                mask = np.array([1, 0])
            if self.QUAD_TYPE == QuadType.TWO_D:
                mask = np.array([1, 0, 1, 0, 1, 0])
            if self.QUAD_TYPE == QuadType.THREE_D:
                mask = np.array([1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 0, 0])
            # Element-wise or to check out-of-bound conditions.
            out_of_bound = np.logical_or(
                self.state < self.state_space.low, self.state > self.state_space.high
            )
            # Mask out un-included dimensions (i.e. velocities)
            out_of_bound = np.any(out_of_bound * mask)
            # Early terminate if needed.
            if out_of_bound:
                return True

        # IROS 2022 - Terminate episode on collision.
        if self.DONE_ON_COLLISION and self.currently_collided:
            return True
        # IROS 2022 - Terminate episode on task completion.
        if self.DONE_ON_COMPLETION and self.task_completed:
            return True

        return False

    def _get_info(self):
        """Generates the info dictionary returned by every call to .step().

        Returns:
            dict: A dictionary with information about the constraints evaluations and violations.

        """
        info = {}
        if self.TASK == Task.STABILIZATION and self.COST == Cost.QUADRATIC:
            info["goal_reached"] = self.goal_reached  # Add boolean flag for the goal being reached.
        # Add MSE.
        state = deepcopy(self.state)
        if self.TASK == Task.STABILIZATION:
            state_error = state - self.X_GOAL
        elif self.TASK == Task.TRAJ_TRACKING:
            # TODO: should use angle wrapping
            # state[4] = normalize_angle(state[4])
            wp_idx = min(self.ctrl_step_counter, self.X_GOAL.shape[0] - 1)
            state_error = state - self.X_GOAL[wp_idx]
        # Filter only relevant dimensions.
        state_error = state_error * self.info_mse_metric_state_weight
        info["mse"] = np.sum(state_error**2)

        # Note: constraint_values and constraint_violations populated in benchmark_env.

        # IROS 2022 - Per-step info.
        # Collisions
        for GATE_OBS_ID in self.GATES_IDS + self.OBSTACLES_IDS + [self.PLANE_ID]:
            ret = p.getContactPoints(
                bodyA=GATE_OBS_ID,
                bodyB=self.DRONE_IDS[0],
                # linkIndexA=-1, linkIndexB=-1,
                physicsClientId=self.PYB_CLIENT,
            )
            if ret:
                info["collision"] = (GATE_OBS_ID, True)
                self.currently_collided = True
                break  # Note: only returning the first collision per step.
        else:
            info["collision"] = (None, False)
            self.currently_collided = False
        #
        # Gates progress (note: allow 0.5 seconds for initial drop if objects are not on the gound).
        if (
            self.pyb_step_counter > 0.5 * self.PYB_FREQ
            and self.NUM_GATES > 0
            and self.current_gate < self.NUM_GATES
        ):
            x, y, _, _, _, rot = self._gates_pose[self.current_gate]
            if self.GATES[self.current_gate][6] == 0:
                height = self.GATE_Z_HIGH  # URDF dependent.
            elif self.GATES[self.current_gate][6] == 1:
                height = self.GATE_Z_LOW  # URDF dependent.
            else:
                raise ValueError("Unknown gate type.")
            half_length = 0.1875  # Obstacle URDF dependent.
            delta_x = 0.05 * np.cos(rot)
            delta_y = 0.05 * np.sin(rot)
            fr = [[x, y, height - half_length]]
            to = [[x, y, height + half_length]]
            for i in [1, 2, 3]:
                fr.append([x + i * delta_x, y + i * delta_y, height - half_length])
                fr.append([x - i * delta_x, y - i * delta_y, height - half_length])
                to.append([x + i * delta_x, y + i * delta_y, height + half_length])
                to.append([x - i * delta_x, y - i * delta_y, height + half_length])
            rays = p.rayTestBatch(
                rayFromPositions=fr, rayToPositions=to, physicsClientId=self.PYB_CLIENT
            )
            self.stepped_through_gate = False
            for r in rays:
                if r[2] < 0.9999:
                    self.current_gate += 1
                    self.stepped_through_gate = True
                    break
        # LSY Drone Racing
        # Always add the nominal gate positions. If any gates are in range, update the position. If
        # any obstacles are in range, also update the obstacle positions.
        VISIBILITY_RANGE = 0.45
        info["gates_pose"] = self.GATES[:, :6].copy()
        info["gates_pose"][:, 2] = np.where(self.GATES[:, 6], self.GATE_Z_LOW, self.GATE_Z_HIGH)
        info["obstacles_pose"] = self.OBSTACLES.copy()
        info["obstacles_pose"][:, 2] = self.OBSTACLE_Z
        info["gates_in_range"] = np.zeros(self.NUM_GATES, dtype=bool)
        info["obstacles_in_range"] = np.zeros(self.n_obstacles, dtype=bool)
        for i in range(self.NUM_GATES):
            closest_points = p.getClosestPoints(
                bodyA=self.GATES_IDS[i],
                bodyB=self.DRONE_IDS[0],
                distance=VISIBILITY_RANGE,
                physicsClientId=self.PYB_CLIENT,
            )
            if len(closest_points) > 0:
                info["gates_pose"][i] = self._gates_pose[i]
                info["gates_in_range"][i] = True
        for i in range(self.n_obstacles):
            closest_points = p.getClosestPoints(
                bodyA=self.OBSTACLES_IDS[i],
                bodyB=self.DRONE_IDS[0],
                distance=VISIBILITY_RANGE,
                physicsClientId=self.PYB_CLIENT,
            )
            if len(closest_points) > 0:
                info["obstacles_pose"][i] = self.obstacle_poses[i]
                info["obstacles_in_range"][i] = True

        info["gates_type"] = self.GATES[:, 6]
        info["current_gate_id"] = self.current_gate if self.current_gate < self.NUM_GATES else -1

        # Final goal position reached
        info["at_goal_position"] = False
        info["task_completed"] = False
        if self.current_gate == self.NUM_GATES:
            if self.QUAD_TYPE == QuadType.THREE_D:
                quad_xyz = np.array([self.state[0], self.state[2], self.state[4]])
                goal_xyz = np.array([self.X_GOAL[0], self.X_GOAL[2], self.X_GOAL[4]])
                if (
                    np.linalg.norm(quad_xyz - goal_xyz)
                    < self.TASK_INFO["stabilization_goal_tolerance"]
                ):
                    self.at_goal_pos = True
                    self.steps_at_goal_pos += 1
                else:
                    self.at_goal_pos = False
                    self.steps_at_goal_pos = 0
                if (
                    self.steps_at_goal_pos > self.CTRL_FREQ * 2
                ):  # Remain near goal position for 2''.
                    self.task_completed = True
                info["at_goal_position"] = self.at_goal_pos
                info["task_completed"] = self.task_completed
            else:
                logger.warning(
                    '"at_goal_position" and "task_completed" are only intended for used with the 3D quadrotor.'
                )

        return info

    def _get_reset_info(self):
        """Generates the info dictionary returned by every call to .reset().

        Returns:
            dict: A dictionary with information about the dynamics and constraints symbolic models.

        """
        info = {}
        info["symbolic_model"] = self.symbolic
        info["nominal_physical_parameters"] = {
            "quadrotor_mass": self.MASS,
            "quadrotor_ixx_inertia": self.J[0, 0],
            "quadrotor_iyy_inertia": self.J[1, 1],
            "quadrotor_izz_inertia": self.J[2, 2],
        }
        info["x_reference"] = self.X_GOAL
        info["u_reference"] = self.U_GOAL
        if self.constraints is not None:
            info["symbolic_constraints"] = self.constraints.get_all_symbolic_models()
        else:
            info["symbolic_constraints"] = {}

        # IROS 2022 - Reset info.
        info["ctrl_timestep"] = self.CTRL_TIMESTEP
        info["ctrl_freq"] = self.CTRL_FREQ
        info["episode_len_sec"] = self.EPISODE_LEN_SEC
        info["quadrotor_kf"] = self.KF
        info["quadrotor_km"] = self.KM
        info["gate_dimensions"] = {
            "tall": {"shape": "square", "height": self.GATE_Z_HIGH, "edge": 0.45},
            "low": {"shape": "square", "height": self.GATE_Z_LOW, "edge": 0.45},
        }
        info["obstacle_dimensions"] = {
            "shape": "cylinder",
            "height": self.OBSTACLE_Z,
            "radius": 0.05,
        }
        info["nominal_gates_pos_and_type"] = self.GATES
        info["nominal_obstacles_pos"] = self.OBSTACLES

        if self.RANDOMIZED_INIT:
            info["initial_state_randomization"] = self.INIT_STATE_RAND_INFO
        else:
            info["initial_state_randomization"] = {}
        if self.RANDOMIZED_INERTIAL_PROP:
            info["inertial_prop_randomization"] = self.INERTIAL_PROP_RAND_INFO
        else:
            info["inertial_prop_randomization"] = {}
        if self.RANDOMIZED_GATES_AND_OBS:
            info["gates_and_obs_randomization"] = self.GATES_AND_OBS_RAND_INFO
        else:
            info["gates_and_obs_randomization"] = {}
        info["disturbances"] = self.DISTURBANCES
        info["pyb_client"] = self.PYB_CLIENT
        info["urdf_dir"] = self.URDF_DIR

        info.update(self._get_info())
        return info

    def set_gate_obstacle_randomization(self, gate_random_dist, obstace_random_dist):
        self.RANDOMIZED_GATES_AND_OBS = True
        update_dict = {
            "gates": {"distrib": "uniform", "low": -gate_random_dist, "high": gate_random_dist},
            "obstacles": {"distrib": "uniform", "low": -obstace_random_dist, "high": obstace_random_dist},
        }
        self.GATES_AND_OBS_RAND_INFO = munchify(update_dict)
    
    def set_init_state_randomization(self, pos_random_dist_x, pos_random_dist_y, pos_random_dist_z):
        self.RANDOMIZED_INIT = True
        update_dict = {
            "init_x": {"distrib": "uniform", "low": -pos_random_dist_x, "high": pos_random_dist_x},
            "init_y": {"distrib": "uniform", "low": -pos_random_dist_y, "high": pos_random_dist_y},
            "init_z": {"distrib": "uniform", "low": -pos_random_dist_z, "high": pos_random_dist_z},
        }
        self.INIT_STATE_RAND_INFO = munchify(update_dict)
