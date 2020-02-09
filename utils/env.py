import gym
from gym import spaces
import numpy as np
import torch
from torch.nn import NLLLoss

from utils.stimuli import VectorStimulusCreator
# from utils.subproc_env import SubprocVecEnv
from utils.vec_env import VecEnv


def create_habituation_world():
    pass


def create_training_choice_world(batch_size):
    """
    "training choice world during which visual stimuli have to be actively moved
    by the mouse; left and right stimuli are presented with equal probability."
    """

    kwargs = dict(
        num_blocks=10,
        stimulus_creator=VectorStimulusCreator(),
        left_bias_probs=[0.5, 0.5],
        right_bias_probs=[0.5, 0.5]
    )

    training_choice_worlds = [ReversalLearningTask(**kwargs)
                              for _ in range(batch_size)]

    return training_choice_worlds


def create_biased_choice_worlds(tensorboard_writer,
                                num_env=7):
    """
    "biased choice world during which visual stimuli have to be actively moved
    by the mouse; left and right stimuli are presented with different probability
    in blocks of trials."
    """

    kwargs = dict(
        num_blocks=100,  # 10
        stimulus_creator=VectorStimulusCreator(),
        left_bias_probs=[0.8, 0.2],
        right_bias_probs=[0.2, 0.8]
    )

    def make_env(kwargs):
        def _f():
            return ReversalLearningTask(**kwargs)
        return _f

    envs = VecEnv(make_env_fn=make_env(kwargs=kwargs), num_env=num_env)
    return envs


class ReversalLearningTask(gym.Env):

    def __init__(self,
                 stimulus_creator,
                 num_blocks=10,
                 block_duration_param=1/60,
                 left_bias_probs=None,
                 right_bias_probs=None):

        self.stimulus_creator = stimulus_creator
        self.num_blocks = num_blocks
        self.block_duration_param = block_duration_param
        self.action_space = spaces.Discrete(2)  # left or right
        self.observation_space = stimulus_creator.observation_space
        self.reward_range = (0, 1)
        self.loss_fn = NLLLoss()

        # store the probability of a stimulus appearing on a particular side
        # within a biased block
        if right_bias_probs is None:
            right_bias_probs = [0.8, 0.2]
        if left_bias_probs is None:
            left_bias_probs = [0.2, 0.8]
        self.side_bias_probs = dict(
            left=left_bias_probs,
            right=right_bias_probs)

        # to (re)initialize the following variables, call self.reset()
        self.num_trials_per_block = None
        self.stimuli_block_number = None
        self.trial_num_within_block = None
        self.stimuli = None
        self.stimuli_sides = None
        self.stimuli_strengths = None
        self.stimuli_preferred_sides = None
        self.rewards = None
        self.actions = None
        self.model_hidden_states = None
        self.current_trial_idx = None
        self.total_num_trials = None

        # TODO: perhaps distinguish between creating new values vs resetting index to repeat previous values

    def close(self):
        pass

    def reset(self):
        """
        (Re)initializes experiment.

        """

        self.num_trials_per_block = [np.random.geometric(p=self.block_duration_param)
                                     for _ in range(self.num_blocks)]

        # truncate to [20, 100]
        for i in range(len(self.num_trials_per_block)):
            num_trials = self.num_trials_per_block[i]
            if num_trials < 20:
                self.num_trials_per_block[i] = 20
            if num_trials > 100:
                self.num_trials_per_block[i] = 100

        self.stimuli_block_number = np.concatenate([
            np.full(shape=nt, fill_value=i) for nt, i
            in zip(self.num_trials_per_block, np.arange(1, 1+len(self.num_trials_per_block)))])
        self.trial_num_within_block = np.concatenate([
            np.arange(1, 1+nt) for nt in self.num_trials_per_block])
        self.total_num_trials = np.sum(self.num_trials_per_block)

        curr_preferred_side = np.random.choice(['left', 'right'])
        blocks_stimuli, blocks_stimuli_sides, blocks_stimuli_strengths = [], [], []
        blocks_preferred_sides = []
        for block_num_trials in self.num_trials_per_block:
            output = self.stimulus_creator.create_block_stimuli(
                block_num_trials=block_num_trials,
                block_side_bias_probabilities=self.side_bias_probs[curr_preferred_side])
            blocks_preferred_sides.append(np.full(
                shape=block_num_trials,
                fill_value=-1. if curr_preferred_side == 'left' else 1.))
            curr_preferred_side = 'right' if curr_preferred_side == 'left' else 'left'
            blocks_stimuli.append(output['sampled_stimuli'])
            blocks_stimuli_sides.append(output['sampled_sides'])
            blocks_stimuli_strengths.append(output['sampled_strengths'])

        # flatten each list of numpy arrays to single torch array
        self.stimuli = torch.from_numpy(np.concatenate(blocks_stimuli))
        self.stimuli_sides = torch.from_numpy(np.concatenate(blocks_stimuli_sides))
        self.stimuli_preferred_sides = torch.from_numpy(np.concatenate(blocks_preferred_sides))
        self.stimuli_strengths = torch.from_numpy(np.concatenate(blocks_stimuli_strengths))
        self.rewards = torch.zeros((self.total_num_trials,))
        self.actions = torch.zeros((self.total_num_trials, 2))
        self.model_hidden_states = []
        self.current_trial_idx = 0

        # create first observation
        step_output = dict(
            stimulus=self.stimuli[self.current_trial_idx].reshape((1, -1)),
            # reward=torch.Tensor([0.]),
            reward=torch.zeros(1).double().requires_grad_(True),
            info=None,
            done=True if self.current_trial_idx == self.total_num_trials else False)

        return step_output

    def step(self,
             model_softmax_output,
             model_hidden):

        # model_softmax_output has shape (batch=1, length=1, 2)
        # reshape action to (batch=1, 2) since loss fn has no notion of sequence
        reward = -self.loss_fn(
            target=(self.stimuli_sides[self.current_trial_idx].reshape((1,)) + 1) / 2,
            input=model_softmax_output.reshape((1, -1)))

        # store record of reward
        self.rewards[self.current_trial_idx] = reward

        # store record of action
        self.actions[self.current_trial_idx] = model_softmax_output.reshape((1, -1))

        # store record of model's hidden state
        self.model_hidden_states.append(model_hidden.detach().numpy())

        # increment trial counter
        self.current_trial_idx += 1

        stimulus = self.stimuli[self.current_trial_idx]
        stimulus_side = self.stimuli_sides[self.current_trial_idx]
        stimulus_strength = self.stimuli_strengths[self.current_trial_idx]

        # store any additional desired information
        info = dict(stimulus_side=stimulus_side,
                    stimulus_strength=stimulus_strength)

        step_output = dict(
            stimulus=stimulus.reshape((1, -1)),
            reward=reward,
            info=info,
            done=True if (self.current_trial_idx + 1) == self.total_num_trials else False)

        return step_output
