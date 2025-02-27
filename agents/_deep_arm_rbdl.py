# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""deluca.agents._deep"""
from numbers import Real

import jax
import jax.numpy as jnp
import numpy as np
import functools

from agents.core import Agent
from envs.core import Env
from utils import Random
import numpy.random as npr

# generic deep controller for 1-dimensional discrete non-negative action space
class Deep_Arm_rbdl(Agent):
    """
    Generic deep controller that uses zero-order methods to train on an
    environment.
    """

    def __init__(
        self,
        env_state_size,
        action_space,
        learning_rate: Real = 0.001,
        gamma: Real = 0.99,
        max_episode_length: int = 500,
        seed: int = 0,
    ) -> None:
        """
        Description: initializes the Deep agent

        Args:
            env (Env): a deluca environment
            learning_rate (Real):
            gamma (Real):
            max_episode_length (int):
            seed (int):

        Returns:
            None
        """
        # Create gym and seed numpy
        self.env_state_size = int(env_state_size)
        self.action_space = action_space
        self.max_episode_length = max_episode_length
        self.lr = learning_rate
        self.gamma = gamma

        self.random = Random(seed)

        self.d_a_d_w = jax.grad(self.__call__,argnums=1)
        self.reset()
        self.value_losses = []

    def reset(self) -> None:
        """
        Description: reset agent

        Args:
            None

        Returns:
            None
        """
        # Init weight
        def init_random_params(scale, layer_sizes, rng=npr.RandomState(0)):
            return [(scale * rng.randn(m, n), scale * rng.randn(n))
                for m, n, in zip(layer_sizes[:-1], layer_sizes[1:])]

        layer_sizes = [self.env_state_size, 128, 128, len(self.action_space)]
        param_scale = 0.1
        self.params = init_random_params(param_scale, layer_sizes)
        #critic weights
        critic_layer_sizes = [self.env_state_size, 128, 128, 1]
        self.value_params =  init_random_params(param_scale, critic_layer_sizes)

        self.W = jax.random.uniform(
            self.random.generate_key(),
            shape=(self.env_state_size, len(self.action_space)),
            minval=0,
            maxval=1,
        )

        # Keep stats for final print of graph
        self.episode_rewards = []

        self.current_episode_length = 0
        self.current_episode_reward = 0
        self.episode_rewards = jnp.zeros(self.max_episode_length)
        self.episode_grads = jnp.zeros((self.max_episode_length, self.W.shape[0], self.W.shape[1]))
        
        # dummy values for attrs, needed to inform scan of traced shapes
        self.state = jnp.zeros((self.env_state_size,))
        self.action = self.action_space[0]
        ones = jnp.ones((len(self.action_space),))
        self.probs = ones * 1/jnp.sum(ones)



    def policy(self, state: jnp.ndarray, params) -> jnp.ndarray:
        """
        Description: Policy that maps state to action parameterized by w

        Args:
            state (jnp.ndarray):
            w (jnp.ndarray):
        """
        activations = state
        for w, b in params[:-1]:
            outputs = jnp.dot(activations, w) + b
            # activations = jnp.tanh(outputs)
            activations = jax.nn.relu(outputs)
        final_w, final_b = params[-1]
        logits = jnp.dot(activations, final_w) + final_b
        # print("logits",logits)

        # z = jnp.dot(state, w)
        # exp = jnp.exp(z)
        # exp = jnp.exp(logits)
        # return exp / jnp.sum(exp)
        return logits

    def value(self, state, params):
        """
        estimate the value of state
        """
        # state = state.flatten() 
        activations = state
        for w, b in params[:-1]:
            outputs = jnp.dot(activations, w) + b
            activations = jnp.tanh(outputs)
        final_w, final_b = params[-1]
        logits = jnp.dot(activations, final_w) + final_b
        return logits[0]

    def softmax_grad(self, softmax: jnp.ndarray) -> jnp.ndarray:
        """
        Description: Vectorized softmax Jacobian

        Args:
            softmax (jnp.ndarray)
        """
        s = softmax.reshape(-1, 1)
        return jnp.diagflat(s) - jnp.dot(s, s.T)

    def __call__(self, state: jnp.ndarray, params):
        """
        Description: provide an action given a state

        Args:
            state (jnp.ndarray):

        Returns:
            jnp.ndarray: action to take
        """
        # print("state",state)
        # print("W",self.W)
        self.state = state
        # self.probs = self.policy(state, params)
        # state = state.flatten() 
        self.action = self.policy(state, params)         
        # self.action = jax.random.choice(
        #     self.random.generate_key(), 
        #     a=self.action_space, 
        #     p=self.probs
        # )
        # self.action = (self.probs[1]-0.5)
        # self.action = jnp.clip((self.probs[1]-0.5)*10, 0, 1)
        return self.action

    def feed(self, reward: Real) -> None:
        """
        Description: compute gradient and save with reward in memory for weight updates

        Args:
            reward (Real):

        Returns:
            None
        """
        dsoftmax = self.softmax_grad(self.probs)[self.action, :]
        dlog = dsoftmax / self.probs[self.action]
        grad = self.state.reshape(-1, 1) @ dlog.reshape(1, -1)

        self.episode_rewards = jax.ops.index_update(
            self.episode_rewards, self.current_episode_length, reward
        )
        self.episode_grads = jax.ops.index_update(
            self.episode_grads, self.current_episode_length, grad
        )
        self.current_episode_length += 1

    def update(self, grads, params, lr):
        """
        Description: update weights
        """
        #get norm square
        total_norm_sqr = 0                
        for (dw,db) in grads:
            # print("previous dw",dw)
            # dw = normalize(dw)
            # db = normalize(db[:,np.newaxis],axis =0).ravel()
            total_norm_sqr += np.linalg.norm(dw) ** 2
            total_norm_sqr += np.linalg.norm(db) ** 2
        # print("grads",grads)

        #scale the gradient
        gradient_clip = 0.2
        scale = min(
            1.0, gradient_clip / (total_norm_sqr**0.5 + 1e-4))

        params = [(w - lr * scale * dw, b - lr * scale * db)
                for (w, b), (dw, db) in zip(params, grads)]

        return params
