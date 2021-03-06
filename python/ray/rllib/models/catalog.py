from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import gym
import numpy as np
import tensorflow as tf
from functools import partial

from ray.tune.registry import RLLIB_MODEL, RLLIB_PREPROCESSOR, \
    _global_registry

from ray.rllib.models.action_dist import (
    Categorical, Deterministic, DiagGaussian, MultiActionDistribution,
    squash_to_range)
from ray.rllib.models.preprocessors import get_preprocessor
from ray.rllib.models.fcnet import FullyConnectedNetwork
from ray.rllib.models.visionnet import VisionNetwork
from ray.rllib.models.lstm import LSTM

# __sphinx_doc_begin__
MODEL_DEFAULTS = {
    # === Built-in options ===
    # Filter config. List of [out_channels, kernel, stride] for each filter
    "conv_filters": None,
    # Nonlinearity for built-in convnet
    "conv_activation": "relu",
    # Nonlinearity for fully connected net (tanh, relu)
    "fcnet_activation": "tanh",
    # Number of hidden layers for fully connected net
    "fcnet_hiddens": [256, 256],
    # For control envs, documented in ray.rllib.models.Model
    "free_log_std": False,
    # Whether to squash the action output to space range
    "squash_to_range": False,

    # == LSTM ==
    # Whether to wrap the model with a LSTM
    "use_lstm": False,
    # Max seq len for training the LSTM, defaults to 20
    "max_seq_len": 20,
    # Size of the LSTM cell
    "lstm_cell_size": 256,

    # == Atari ==
    # Whether to enable framestack for Atari envs
    "framestack": True,
    # Final resized frame dimension
    "dim": 84,
    # Pytorch conv requires images to be channel-major
    "channel_major": False,
    # (deprecated) Converts ATARI frame to 1 Channel Grayscale image
    "grayscale": False,
    # (deprecated) Changes frame to range from [-1, 1] if true
    "zero_mean": True,

    # === Options for custom models ===
    # Name of a custom preprocessor to use
    "custom_preprocessor": None,
    # Name of a custom model to use
    "custom_model": None,
    # Extra options to pass to the custom classes
    "custom_options": {},
}

# __sphinx_doc_end__


class ModelCatalog(object):
    """Registry of models, preprocessors, and action distributions for envs.

    Examples:
        >>> prep = ModelCatalog.get_preprocessor(env)
        >>> observation = prep.transform(raw_observation)

        >>> dist_cls, dist_dim = ModelCatalog.get_action_dist(
                env.action_space, {})
        >>> model = ModelCatalog.get_model(inputs, dist_dim, options)
        >>> dist = dist_cls(model.outputs)
        >>> action = dist.sample()
    """

    @staticmethod
    def get_action_dist(action_space, config, dist_type=None):
        """Returns action distribution class and size for the given action space.

        Args:
            action_space (Space): Action space of the target gym env.
            config (dict): Optional model config.
            dist_type (str): Optional identifier of the action distribution.

        Returns:
            dist_class (ActionDistribution): Python class of the distribution.
            dist_dim (int): The size of the input vector to the distribution.
        """

        config = config or MODEL_DEFAULTS
        if isinstance(action_space, gym.spaces.Box):
            if dist_type is None:
                dist = DiagGaussian
                if config.get("squash_to_range"):
                    dist = squash_to_range(dist, action_space.low,
                                           action_space.high)
                return dist, action_space.shape[0] * 2
            elif dist_type == "deterministic":
                return Deterministic, action_space.shape[0]
        elif isinstance(action_space, gym.spaces.Discrete):
            return Categorical, action_space.n
        elif isinstance(action_space, gym.spaces.Tuple):
            child_dist = []
            input_lens = []
            for action in action_space.spaces:
                dist, action_size = ModelCatalog.get_action_dist(
                    action, config)
                child_dist.append(dist)
                input_lens.append(action_size)
            return partial(
                MultiActionDistribution,
                child_distributions=child_dist,
                action_space=action_space,
                input_lens=input_lens), sum(input_lens)

        raise NotImplementedError("Unsupported args: {} {}".format(
            action_space, dist_type))

    @staticmethod
    def get_action_placeholder(action_space):
        """Returns an action placeholder that is consistent with the action space

        Args:
            action_space (Space): Action space of the target gym env.
        Returns:
            action_placeholder (Tensor): A placeholder for the actions
        """

        # TODO(ekl) are list spaces valid?
        if isinstance(action_space, list):
            action_space = gym.spaces.Tuple(action_space)

        if isinstance(action_space, gym.spaces.Box):
            return tf.placeholder(
                tf.float32, shape=(None, action_space.shape[0]), name="action")
        elif isinstance(action_space, gym.spaces.Discrete):
            return tf.placeholder(tf.int64, shape=(None, ), name="action")
        elif isinstance(action_space, gym.spaces.Tuple):
            size = 0
            all_discrete = True
            for i in range(len(action_space.spaces)):
                if isinstance(action_space.spaces[i], gym.spaces.Discrete):
                    size += 1
                else:
                    all_discrete = False
                    size += np.product(action_space.spaces[i].shape)
            return tf.placeholder(
                tf.int64 if all_discrete else tf.float32,
                shape=(None, size),
                name="action")
        else:
            raise NotImplementedError("action space {}"
                                      " not supported".format(action_space))

    @staticmethod
    def get_model(inputs, num_outputs, options, state_in=None, seq_lens=None):
        """Returns a suitable model conforming to given input and output specs.

        Args:
            inputs (Tensor): The input tensor to the model.
            num_outputs (int): The size of the output vector of the model.
            options (dict): Optional args to pass to the model constructor.
            state_in (list): Optional RNN state in tensors.
            seq_in (Tensor): Optional RNN sequence length tensor.

        Returns:
            model (Model): Neural network model.
        """

        options = options or MODEL_DEFAULTS
        model = ModelCatalog._get_model(inputs, num_outputs, options, state_in,
                                        seq_lens)

        if options.get("use_lstm"):
            model = LSTM(model.last_layer, num_outputs, options, state_in,
                         seq_lens)

        return model

    @staticmethod
    def _get_model(inputs, num_outputs, options, state_in, seq_lens):
        if options.get("custom_model"):
            model = options["custom_model"]
            print("Using custom model {}".format(model))
            return _global_registry.get(RLLIB_MODEL, model)(
                inputs,
                num_outputs,
                options,
                state_in=state_in,
                seq_lens=seq_lens)

        obs_rank = len(inputs.shape) - 1

        if obs_rank > 1:
            return VisionNetwork(inputs, num_outputs, options)

        return FullyConnectedNetwork(inputs, num_outputs, options)

    @staticmethod
    def get_torch_model(input_shape, num_outputs, options=None):
        """Returns a PyTorch suitable model. This is currently only supported
        in A3C.

        Args:
            input_shape (tuple): The input shape to the model.
            num_outputs (int): The size of the output vector of the model.
            options (dict): Optional args to pass to the model constructor.

        Returns:
            model (Model): Neural network model.
        """
        from ray.rllib.models.pytorch.fcnet import (FullyConnectedNetwork as
                                                    PyTorchFCNet)
        from ray.rllib.models.pytorch.visionnet import (VisionNetwork as
                                                        PyTorchVisionNet)

        options = options or MODEL_DEFAULTS
        if options.get("custom_model"):
            model = options["custom_model"]
            print("Using custom torch model {}".format(model))
            return _global_registry.get(RLLIB_MODEL, model)(
                input_shape, num_outputs, options)

        # TODO(alok): fix to handle Discrete(n) state spaces
        obs_rank = len(input_shape) - 1

        if obs_rank > 1:
            return PyTorchVisionNet(input_shape, num_outputs, options)

        # TODO(alok): overhaul PyTorchFCNet so it can just
        # take input shape directly
        return PyTorchFCNet(input_shape[0], num_outputs, options)

    @staticmethod
    def get_preprocessor(env, options=None):
        """Returns a suitable processor for the given environment.

        Args:
            env (gym.Env): The gym environment to preprocess.
            options (dict): Options to pass to the preprocessor.

        Returns:
            preprocessor (Preprocessor): Preprocessor for the env observations.
        """
        options = options or MODEL_DEFAULTS
        for k in options.keys():
            if k not in MODEL_DEFAULTS:
                raise Exception("Unknown config key `{}`, all keys: {}".format(
                    k, list(MODEL_DEFAULTS)))

        if options.get("custom_preprocessor"):
            preprocessor = options["custom_preprocessor"]
            print("Using custom preprocessor {}".format(preprocessor))
            return _global_registry.get(RLLIB_PREPROCESSOR, preprocessor)(
                env.observation_space, options)

        preprocessor = get_preprocessor(env.observation_space)
        return preprocessor(env.observation_space, options)

    @staticmethod
    def get_preprocessor_as_wrapper(env, options=None):
        """Returns a preprocessor as a gym observation wrapper.

        Args:
            env (gym.Env): The gym environment to wrap.
            options (dict): Options to pass to the preprocessor.

        Returns:
            wrapper (gym.ObservationWrapper): Preprocessor in wrapper form.
        """

        options = options or MODEL_DEFAULTS
        preprocessor = ModelCatalog.get_preprocessor(env, options)
        return _RLlibPreprocessorWrapper(env, preprocessor)

    @staticmethod
    def register_custom_preprocessor(preprocessor_name, preprocessor_class):
        """Register a custom preprocessor class by name.

        The preprocessor can be later used by specifying
        {"custom_preprocessor": preprocesor_name} in the model config.

        Args:
            preprocessor_name (str): Name to register the preprocessor under.
            preprocessor_class (type): Python class of the preprocessor.
        """
        _global_registry.register(RLLIB_PREPROCESSOR, preprocessor_name,
                                  preprocessor_class)

    @staticmethod
    def register_custom_model(model_name, model_class):
        """Register a custom model class by name.

        The model can be later used by specifying {"custom_model": model_name}
        in the model config.

        Args:
            model_name (str): Name to register the model under.
            model_class (type): Python class of the model.
        """
        _global_registry.register(RLLIB_MODEL, model_name, model_class)


class _RLlibPreprocessorWrapper(gym.ObservationWrapper):
    """Adapts a RLlib preprocessor for use as an observation wrapper."""

    def __init__(self, env, preprocessor):
        super(_RLlibPreprocessorWrapper, self).__init__(env)
        self.preprocessor = preprocessor

        from gym.spaces.box import Box
        self.observation_space = Box(
            -1.0, 1.0, preprocessor.shape, dtype=np.float32)

    def observation(self, observation):
        return self.preprocessor.transform(observation)
