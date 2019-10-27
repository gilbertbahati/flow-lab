import json

import ray
try:
    from ray.rllib.agents.agent import get_agent_class
except ImportError:
    from ray.rllib.agents.registry import get_agent_class
from ray.tune import run_experiments
from ray.tune.registry import register_env

# from flow.utils.registry import make_create_env
from utils import make_create_env, unscaledMergePOEnv
from flow.utils.rllib import FlowParamsEncoder
from flow.controllers import RLController, IDMController
from flow.core.experiment import Experiment
from flow.core.params import SumoParams, EnvParams, InitialConfig, InFlows, NetParams
from flow.core.params import VehicleParams, SumoCarFollowingParams
from flow.scenarios.merge import ADDITIONAL_NET_PARAMS

FLOW_RATE = 2000
HORIZON = 1800
N_ROLLOUTS = 10
N_CPUS = 2


def setup_inflows(rl_penetration):
    # Vehicles are introduced from both sides of merge, with RL vehicles entering
    # from the highway portion as well
    inflow = InFlows()
    inflow.add(
        veh_type="human",
        edge="inflow_highway",
        vehs_per_hour=(1 - rl_penetration) * FLOW_RATE,
        depart_lane="free",
        depart_speed=10)
    inflow.add(
        veh_type="rl",
        edge="inflow_highway",
        vehs_per_hour=rl_penetration * FLOW_RATE,
        depart_lane="free",
        depart_speed=10)
    inflow.add(
        veh_type="human",
        edge="inflow_merge",
        vehs_per_hour=100,
        depart_lane="free",
        depart_speed=7.5)
    return inflow


# Setup vehicle types
vehicles = VehicleParams()
vehicles.add(
    veh_id="human",
    acceleration_controller=(IDMController, {
        "noise": 0.2
    }),
    car_following_params=SumoCarFollowingParams(
        speed_mode="obey_safe_speed",
    ),
    num_vehicles=5)
vehicles.add(
    veh_id="rl",
    acceleration_controller=(RLController, {}),
    car_following_params=SumoCarFollowingParams(
        speed_mode="obey_safe_speed",
    ),
    num_vehicles=0)

# Set parameters for the network
additional_net_params = ADDITIONAL_NET_PARAMS.copy()
additional_net_params["pre_merge_length"] = 600
additional_net_params["post_merge_length"] = 100
additional_net_params["merge_lanes"] = 1
additional_net_params["highway_lanes"] = 1


def get_flow_params(rl_penetration):
    return dict(
        # name of the experiment
        exp_tag="dissipating_waves",

        # name of the flow environment the experiment is running on
        env_name=unscaledMergePOEnv,

        # name of the scenario class the experiment is running on
        scenario="MergeScenario",

        # simulator that is used by the experiment
        simulator='traci',

        # sumo-related parameters (see flow.core.params.SumoParams)
        sim=SumoParams(
            sim_step=0.2,
            render=False,
            restart_instance=True,
        ),

        # environment related parameters (see flow.core.params.EnvParams)
        env=EnvParams(
            horizon=HORIZON,
            sims_per_step=5,
            warmup_steps=0,
            additional_params={
                "max_accel": 3,
                "max_decel": 3,
                "target_velocity": 25,
                # dunno where the number comes from
                "num_rl": round(rl_penetration*100/2),
            },
        ),

        # network-related parameters (see flow.core.params.NetParams and the
        # scenario's documentation or ADDITIONAL_NET_PARAMS component)
        net=NetParams(
            inflows=setup_inflows(rl_penetration),
            additional_params=additional_net_params,
        ),

        # vehicles to be placed in the network at the start of a rollout (see
        # flow.core.params.VehicleParams)
        veh=vehicles,

        # parameters specifying the positioning of vehicles upon initialization/
        # reset (see flow.core.params.InitialConfig)
        initial=InitialConfig(),
    )


def setup_exps():
    """Return the relevant components of an RLlib experiment.

    Returns
    -------
    str
        name of the training algorithm
    str
        name of the gym environment to be trained
    dict
        training configuration parameters
    """
    alg_run = "PPO"

    agent_cls = get_agent_class(alg_run)
    config = agent_cls._default_config.copy()
    config["num_workers"] = N_CPUS
    config["train_batch_size"] = HORIZON * N_ROLLOUTS
    config["gamma"] = 0.999  # discount rate
    config["model"].update({"fcnet_hiddens": [32, 32, 32]})
    config["use_gae"] = True
    config["lambda"] = 0.97
    config["kl_target"] = 0.02
    config["num_sgd_iter"] = 10
    config['clip_actions'] = False  # FIXME(ev) temporary ray bug
    config["horizon"] = HORIZON

    # save the flow params for replay
    flow_json = json.dumps(
        flow_params, cls=FlowParamsEncoder, sort_keys=True, indent=4)
    config['env_config']['flow_params'] = flow_json
    config['env_config']['run'] = alg_run

    create_env, gym_name = make_create_env(params=flow_params, version=0)

    # Register as rllib env
    register_env(gym_name, create_env)
    return alg_run, gym_name, config


# Naming the outputs nicely, and placing them all in one directory
def trial_string(paramval, trial):
    return "{}_penetration_{:.3f}".format(trial.trainable_name, paramval)


if __name__ == "__main__":
    import functools
    ray.init(num_cpus=N_CPUS + 1, redirect_output=False)

    for rl_penetration in [0.025, 0.05, 0.1]:
        flow_params = get_flow_params(rl_penetration)
        alg_run, gym_name, config = setup_exps()

        trials = run_experiments({
            flow_params["exp_tag"]: {
                "run": alg_run,
                "env": gym_name,
                "config": {
                    **config
                },
                "checkpoint_freq": 20,
                "checkpoint_at_end": True,
                "max_failures": 999,
                "stop": {
                    "training_iteration": 200,
                },
                "trial_name_creator": functools.partial(trial_string, rl_penetration),
            }
        })
