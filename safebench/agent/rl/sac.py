'''
Date: 2023-01-31 22:23:17
LastEditTime: 2023-04-01 16:00:31
Description:
    Copyright (c) 2022-2023 Safebench Team

    This work is licensed under the terms of the MIT license.
    For a copy, see <https://opensource.org/licenses/MIT>
'''

import os

import torch
import torch.nn as nn
import torch.optim as optim
from fnmatch import fnmatch
from torch.distributions import Normal

from safebench.util.torch_util import CUDA, CPU, kaiming_init
from safebench.agent.base_policy import BasePolicy

def make_conv():
    return nn.Sequential(
        nn.Conv2d(3, 32, kernel_size=4, stride=1, padding=0), # 128
        nn.ReLU(),
        nn.Conv2d(32, 32, kernel_size=3, stride=2, padding=0), # 62
        nn.ReLU(),
        nn.Conv2d(32, 16, kernel_size=3, stride=2, padding=0), # 30
        nn.ReLU(),
        nn.Conv2d(16, 8, kernel_size=3, stride=2, padding=0), # 14
        nn.ReLU(),
        nn.Flatten(),
        nn.Linear(14 * 14 * 8, 256),
        nn.ReLU(),
    )

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Actor, self).__init__()
        self.conv = make_conv()
        self.fc1 = nn.Linear(256+4, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc_mu = nn.Linear(256, action_dim)
        self.fc_std = nn.Linear(256, action_dim)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()
        self.softplus = nn.Softplus()
        self.min_val = 1e-3
        self.apply(kaiming_init)

    def forward(self, x):
        x1 = x[:,:-4]
        x2 = x[:,-4:]
        x1 = x1.reshape(-1, 3, 128, 128)
        x2 = x2.reshape(-1, 4)

        e1 = self.conv(x1)
        x = self.relu(self.fc1(torch.cat((e1, x2), -1)))
        x = self.relu(self.fc2(x))
        mu = self.tanh(self.fc_mu(x))
        logstd = self.fc_std(x)
        return mu, logstd


class Critic(nn.Module):
    def __init__(self, state_dim):
        super(Critic, self).__init__()
        self.conv = make_conv()
        self.fc1 = nn.Linear(256+4, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)
        self.relu = nn.ReLU()
        self.apply(kaiming_init)

    def forward(self, x):
        x1 = x[:,:-4]
        x2 = x[:,-4:]
        x1 = x1.reshape(-1, 3, 128, 128)
        x2 = x2.reshape(-1, 4)

        e1 = self.conv(x1)
        x = self.relu(self.fc1(torch.cat((e1, x2), -1)))
        x = self.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class Q(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Q, self).__init__()
        self.action_dim = action_dim
        self.conv = make_conv()
        self.state_dim = 256 + 4
        self.fc1 = nn.Linear(256+4+action_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)
        self.relu = nn.ReLU()
        self.apply(kaiming_init)

    def forward(self, x, a):
        x1 = x[:,:-4]
        x2 = x[:,-4:]
        x1 = x1.reshape(-1, 3, 128, 128)
        x2 = x2.reshape(-1, 4)

        e1 = self.conv(x1)

        a = a.reshape(-1, self.action_dim)
        x = torch.cat((e1, x2, a), -1) # combination x and a
        x = self.relu(self.fc1(x))
        x = self.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class SAC(BasePolicy):
    name = 'SAC'
    type = 'offpolicy'

    def __init__(self, config, logger):
        self.logger = logger

        self.use_suppression = True #config['use_suppression']
        self.use_recovery = True
        if self.use_suppression:
            assert self.use_recovery

        self.lam = 2.0
        #assert self.use_suppression

        self.buffer_start_training = config['buffer_start_training']
        self.lr = config['lr']
        self.continue_episode = 0
        self.state_dim = config['ego_state_dim']
        self.action_dim = config['ego_action_dim']
        self.min_Val = torch.tensor(config['min_Val']).float()
        self.batch_size = config['batch_size']
        self.update_iteration = config['update_iteration']
        self.gamma = config['gamma']
        self.tau = config['tau']

        self.model_id = config['model_id']
        self.model_path = os.path.join(config['ROOT_DIR'], config['model_path'])
        if not os.path.exists(self.model_path):
            os.makedirs(self.model_path)

        # create models
        self.policy_net = CUDA(Actor(self.state_dim, self.action_dim))
        self.value_net = CUDA(Critic(self.state_dim))
        self.Q_net = CUDA(Q(self.state_dim, self.action_dim))
        self.Target_value_net = CUDA(Critic(self.state_dim))
        if self.use_recovery:
            self.value_risk_net = CUDA(Critic(self.state_dim))
            self.policy_recovery_net = CUDA(Actor(self.state_dim, self.action_dim))
            self.Q_risk_net = CUDA(Q(self.state_dim, self.action_dim))
            self.Target_value_risk_net = CUDA(Critic(self.state_dim))

        # create optimizer
        self.policy_optimizer = optim.Adam(self.policy_net.parameters(), lr=self.lr)
        self.value_optimizer = optim.Adam(self.value_net.parameters(), lr=self.lr)
        self.Q_optimizer = optim.Adam(self.Q_net.parameters(), lr=self.lr)
        if self.use_recovery:
            self.policy_recovery_optimizer = optim.Adam(self.policy_recovery_net.parameters(), lr=self.lr)
            self.value_risk_optimizer = optim.Adam(self.value_risk_net.parameters(), lr=self.lr)
            self.Q_risk_optimizer = optim.Adam(self.Q_risk_net.parameters(), lr=self.lr)

        # define loss function
        self.value_criterion = nn.MSELoss()
        self.Q_criterion = nn.MSELoss()
        if self.use_recovery:
            self.value_risk_criterion = nn.MSELoss()
            self.Q_risk_criterion = nn.MSELoss()

        self.risk_thresh = 0.1

        # copy parameters
        for target_param, param in zip(self.Target_value_net.parameters(), self.value_net.parameters()):
            target_param.data.copy_(param.data)
        if self.use_recovery:
            for target_param, param in zip(self.Target_value_risk_net.parameters(), self.value_risk_net.parameters()):
                target_param.data.copy_(param.data)

        self.mode = 'train'

    def set_mode(self, mode):
        self.mode = mode
        if mode == 'train':
            self.policy_net.train()
            self.value_net.train()
            self.Q_net.train()
            if self.use_recovery:
                self.policy_recovery_net.train()
                self.value_risk_net.train()
                self.Q_risk_net.train()
        elif mode == 'eval':
            self.policy_net.eval()
            self.value_net.eval()
            self.Q_net.eval()
            if self.use_recovery:
                self.policy_recovery_net.eval()
                self.value_risk_net.eval()
                self.Q_risk_net.eval()
        else:
            raise ValueError(f'Unknown mode {mode}')

    def get_action(self, state, infos, deterministic=False):
        state = CUDA(torch.FloatTensor(state))
        mu_task, log_sigma_task = self.policy_net(state)

        def sample(mu, log_sigma):
            if deterministic:
                a = mu
            else:
                sigma = torch.exp(log_sigma)
                dist = Normal(mu, sigma)
                z = dist.sample()
                a = torch.tanh(z)
            return a

        task_action = sample(mu_task, log_sigma_task)

        if self.use_recovery:
            risk = self.Q_risk_net(state, task_action)
            if risk.detach().cpu().numpy().ravel()[0] < -self.risk_thresh:
                mu_rec, log_sigma_rec = self.policy_recovery_net(state)
                action = sample(mu_rec, log_sigma_rec)
            else:
                action = task_action
        else:
            action = task_action

        return CPU(task_action), CPU(action)

    def get_action_log_prob(self, state):
        batch_mu, batch_log_sigma = self.policy_net(state)
        batch_sigma = torch.exp(batch_log_sigma)
        dist = Normal(batch_mu, batch_sigma)
        z = dist.sample()
        task_action = torch.tanh(z)

        zeros = torch.zeros_like(task_action)

        task_log_prob = dist.log_prob(z) - torch.log(torch.maximum(1 - task_action.pow(2) + self.min_Val, zeros) + 1e-3)
        # when action has more than 1 dimensions, we should sum up the log likelihood
        task_log_prob = torch.sum(task_log_prob, dim=1, keepdim=True)

        if self.use_recovery:
            batch_mu_rec, batch_log_sigma_rec = self.policy_recovery_net(state)
            batch_sigma_rec = torch.exp(batch_log_sigma_rec)
            dist_rec = Normal(batch_mu_rec, batch_sigma_rec)
            z_rec = dist_rec.sample()
            recovery_action = torch.tanh(z_rec)

            recovery_log_prob = dist_rec.log_prob(z_rec) - torch.log(torch.maximum(1 - recovery_action.pow(2) + self.min_Val, zeros) + 1e-3)
            recovery_log_prob = torch.sum(recovery_log_prob, dim=1, keepdim=True)

            recovery_log_prob_2 = dist_rec.log_prob(z.detach()) - torch.log(torch.maximum(1. - task_action.detach().pow(2) + self.min_Val, zeros) + 1e-3)
            recovery_log_prob_2 = torch.sum(recovery_log_prob, dim=1, keepdim=True)

            risk = self.Q_risk_net(state, task_action).detach()
            recovery_active = risk.detach() < -self.risk_thresh
            recovery_active = recovery_active.float()
            action = recovery_action #* recovery_active + (1. - recovery_active) * task_action.detach()
            log_prob = recovery_log_prob #* recovery_active + (1. - recovery_active) * recovery_log_prob_2
        else:
            action = task_action
            log_prob = task_log_prob

        return {
            "task_action": task_action,
            "task_log_prob": task_log_prob,
            "mixed_action": action,
            "mixed_log_prob": log_prob,
        }

        return action, log_prob, z, batch_mu, batch_log_sigma

    def train(self, replay_buffer):
        if replay_buffer.buffer_len < self.buffer_start_training:
            return

        for _ in range(self.update_iteration):
            # sample replay buffer
            batch = replay_buffer.sample(self.batch_size)
            bn_s = CUDA(torch.FloatTensor(batch['state']))
            bn_a_task = CUDA(torch.FloatTensor(batch['task_action']))
            bn_a_mixed = CUDA(torch.FloatTensor(batch['mixed_action']))
            bn_r = CUDA(torch.FloatTensor(batch['reward'])).unsqueeze(-1) # [B, 1]
            bn_r_risk = CUDA(-torch.FloatTensor(batch['risk'])).unsqueeze(-1)
            bn_s_ = CUDA(torch.FloatTensor(batch['n_state']))
            bn_d = CUDA(torch.FloatTensor(1-batch['done'])).unsqueeze(-1) # [B, 1]

            if not self.use_recovery:
                bn_r = bn_r + self.lam * bn_r_risk

            target_value = self.Target_value_net(bn_s_)
            next_q_value = bn_r + bn_d * self.gamma * target_value
            if self.use_recovery:
                target_value_risk = self.Target_value_risk_net(bn_s_)
                next_q_risk_value = bn_r_risk + bn_d * self.gamma * target_value_risk
                max_terminal = torch.clamp(bn_r_risk / (1. - self.gamma) * (1. - bn_d) - bn_r_risk, 0.0, 50. / (1. - self.gamma))
                next_q_risk_value += max_terminal

            expected_value = self.value_net(bn_s)
            expected_Q = self.Q_net(bn_s, bn_a_task)
            if self.use_recovery:
                expected_value_risk = self.value_risk_net(bn_s)
                expected_Q_risk = self.Q_risk_net(bn_s, bn_a_mixed)

            actions_log_probs = self.get_action_log_prob(bn_s)
            expected_new_Q = self.Q_net(bn_s, actions_log_probs["task_action"])
            if self.use_recovery:
                expected_new_Q_risk = self.Q_risk_net(bn_s, actions_log_probs["mixed_action"])

            next_value = expected_new_Q - actions_log_probs["task_log_prob"]
            if self.use_recovery:
                next_value_risk = expected_new_Q_risk - actions_log_probs["mixed_log_prob"]

            # !!! Note that the actions are sampled according to the current policy, instead of replay buffer. (From original paper)
            V_loss = self.value_criterion(expected_value, next_value.detach())  # J_V
            V_loss = V_loss.mean()
            if self.use_recovery:
                V_risk_loss = self.value_risk_criterion(expected_value_risk, next_value_risk.detach())
                V_risk_loss = V_risk_loss.mean()

            # Single Q_net this is different from original paper!!!
            Q_loss = self.Q_criterion(expected_Q, next_q_value.detach()) # J_Q
            Q_loss = Q_loss.mean()
            if self.use_recovery:
                Q_risk_loss = self.Q_risk_criterion(expected_Q_risk, next_q_risk_value.detach())
                Q_risk_loss = Q_risk_loss.mean()

            log_policy_target = expected_new_Q - expected_value
            if self.use_recovery:
                log_policy_recovery_target = expected_new_Q_risk - expected_value_risk
                if self.use_suppression:
                    suppression_weight = torch.ones_like(expected_new_Q_risk) * 10.0
                    gate = -expected_new_Q_risk * suppression_weight
                    gate = torch.clamp(gate * 2, 0.25, 0.75).detach()

                    #print(gate, log_policy_target, log_policy_recovery_target)
                    log_policy_target = log_policy_target * (1. - gate) + gate * log_policy_recovery_target.detach()

            pi_loss = actions_log_probs["task_log_prob"] * (actions_log_probs["task_log_prob"] - log_policy_target).detach()

            pi_loss = pi_loss.mean()
            if self.use_recovery:
                pi_recovery_loss = actions_log_probs["mixed_log_prob"] * (actions_log_probs["mixed_log_prob"] - log_policy_recovery_target).detach()
                pi_recovery_loss = pi_recovery_loss.mean()

            # mini batch gradient descent
            self.value_optimizer.zero_grad()
            V_loss.backward(retain_graph=True)
            nn.utils.clip_grad_norm_(self.value_net.parameters(), 0.5)
            self.value_optimizer.step()

            self.Q_optimizer.zero_grad()
            Q_loss.backward(retain_graph=True)
            nn.utils.clip_grad_norm_(self.Q_net.parameters(), 0.5)
            self.Q_optimizer.step()

            self.policy_optimizer.zero_grad()
            pi_loss.backward(retain_graph=True)
            nn.utils.clip_grad_norm_(self.policy_net.parameters(), 0.5)
            self.policy_optimizer.step()

            if self.use_recovery:
                self.value_risk_optimizer.zero_grad()
                V_risk_loss.backward(retain_graph=True)
                nn.utils.clip_grad_norm_(self.value_risk_net.parameters(), 0.5)
                self.value_risk_optimizer.step()

                self.Q_risk_optimizer.zero_grad()
                Q_risk_loss.backward(retain_graph=True)
                nn.utils.clip_grad_norm_(self.Q_risk_net.parameters(), 0.5)
                self.Q_risk_optimizer.step()

                self.policy_recovery_optimizer.zero_grad()
                pi_recovery_loss.backward(retain_graph=True)
                nn.utils.clip_grad_norm_(self.policy_recovery_net.parameters(), 0.5)
                self.policy_recovery_optimizer.step()

            # soft update
            for target_param, param in zip(self.Target_value_net.parameters(), self.value_net.parameters()):
                target_param.data.copy_(target_param * (1 - self.tau) + param * self.tau)
            if self.use_recovery:
                for target_param, param in zip(self.Target_value_risk_net.parameters(), self.value_risk_net.parameters()):
                    target_param.data.copy_(target_param * (1 - self.tau) + param * self.tau)

    def save_model(self, episode):
        states = {
            'policy_net': self.policy_net.state_dict(),
            'value_net': self.value_net.state_dict(),
            'Q_net': self.Q_net.state_dict(),
        }
        if self.use_recovery:
            states.update({
                'policy_recovery_net': self.policy_recovery_net.state_dict(),
                'value_risk_net': self.value_risk_net.state_dict(),
                'Q_risk_net': self.Q_risk_net.state_dict(),
            })
        filepath = os.path.join(self.model_path, f'model.sac.{self.model_id}.{episode:04}.torch')
        self.logger.log(f'>> Saving {self.name} model to {filepath}')
        with open(filepath, 'wb+') as f:
            torch.save(states, f)

    def load_model(self, episode=None):
        if episode is None:
            episode = -1
            for _, _, files in os.walk(self.model_path):
                for name in files:
                    if fnmatch(name, "*torch"):
                        cur_episode = int(name.split(".")[-2])
                        if cur_episode > episode:
                            episode = cur_episode
        filepath = os.path.join(self.model_path, f'model.sac.{self.model_id}.{episode:04}.torch')
        if os.path.isfile(filepath):
            self.logger.log(f'>> Loading {self.name} model from {filepath}')
            with open(filepath, 'rb') as f:
                checkpoint = torch.load(f)
            self.policy_net.load_state_dict(checkpoint['policy_net'])
            self.value_net.load_state_dict(checkpoint['value_net'])
            self.Q_net.load_state_dict(checkpoint['Q_net'])
            self.continue_episode = episode
            if self.use_recovery:
                self.policy_recovery_net.load_state_dict(checkpoint['policy_recovery_net'])
                self.value_risk_net.load_state_dict(checkpoint['value_risk_net'])
                self.Q_risk_net.load_state_dict(checkpoint['Q_risk_net'])
        else:
            self.logger.log(f'>> No {self.name} model found at {filepath}', 'red')
