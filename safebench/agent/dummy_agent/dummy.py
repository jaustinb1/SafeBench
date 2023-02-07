'''
Author: Wenhao Ding
Email: wenhaod@andrew.cmu.edu
Date: 2023-01-30 22:30:20
LastEditTime: 2023-02-06 19:44:06
Description: 
'''

import numpy as np


class DummyEgo(object):
    """ This is just an example for testing, whcih always goes straight. """
    def __init__(self, config):
        self.action_dim = config['action_dim']
        self.model_path = config['model_path']
        self.mode = 'train'

    def get_action(self, obs):
        # the input should be formed into a batch, the return action should also be a batch
        batch_size = len(obs)
        action = np.random.randn(batch_size, self.action_dim)
        action[:, 0] = 0.5
        action[:, 1] = 0
        return action

    def load_model(self):
        pass

    def set_mode(self, mode):
        self.mode = mode

    def update(self):
        pass
