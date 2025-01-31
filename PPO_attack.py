import os
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from torch.distributions import Normal
from torchvision.utils import save_image
from generator import Generator
from classify import *
from utils import *

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def init_weights(m):
    if isinstance(m, nn.Linear):
        nn.init.orthogonal_(m.weight)
        m.bias.data.fill_(0.0)

class Actor(nn.Module):
    def __init__(self, state_size, action_size, hidden_size=256):
        super(Actor, self).__init__()
        self.fc1 = nn.Linear(state_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.mean = nn.Linear(hidden_size, action_size)
        # Initialize with higher std for better exploration
        self.log_std = nn.Parameter(torch.ones(action_size) * -0.5)
        
        self.apply(init_weights)
        
    def forward(self, state):
        x = F.relu(self.fc1(state))
        x = F.relu(self.fc2(x))
        mean = self.mean(x)
        std = self.log_std.exp()
        return mean, std
        
    def get_action(self, state):
        mean, std = self.forward(state)
        dist = Normal(mean, std)
        action = dist.sample()
        return action, dist.log_prob(action).sum(dim=-1)

class Critic(nn.Module):
    def __init__(self, state_size, hidden_size=256):
        super(Critic, self).__init__()
        self.fc1 = nn.Linear(state_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, 1)
        
        self.apply(init_weights)
        
    def forward(self, state):
        x = F.relu(self.fc1(state))
        x = F.relu(self.fc2(x))
        value = self.fc3(x)
        return value

class PPOMemory:
    def __init__(self, batch_size=32):
        self.states = []
        self.actions = []
        self.rewards = []
        self.values = []
        self.log_probs = []
        self.batch_size = batch_size
        
    def generate_batches(self):
        n_states = len(self.states)
        batch_start = np.arange(0, n_states, self.batch_size)
        indices = np.arange(n_states, dtype=np.int64)
        np.random.shuffle(indices)
        batches = [indices[i:i+self.batch_size] for i in batch_start]
        return batches
    
    def store(self, state, action, reward, value, log_prob):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.values.append(value)
        self.log_probs.append(log_prob)
        
    def clear(self):
        self.states = []
        self.actions = []
        self.rewards = []
        self.values = []
        self.log_probs = []

class PPOAgent:
    def __init__(self, state_size, action_size, hidden_size=256, 
                 lr=3e-4, gamma=0.99, gae_lambda=0.95, 
                 clip_epsilon=0.2, c1=0.5, c2=0.01,
                 batch_size=64, n_epochs=10,
                 action_std_init=0.6):
        self.actor = Actor(state_size, action_size, hidden_size).to(device)
        self.critic = Critic(state_size, hidden_size).to(device)
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=lr)
        self.critic_optimizer = optim.Adam(self.critic.parameters(), lr=lr)
        
        self.memory = PPOMemory(batch_size)
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.clip_epsilon = clip_epsilon
        self.c1 = c1  # Value loss coefficient
        self.c2 = c2  # Entropy coefficient
        self.n_epochs = n_epochs
        self.action_std = action_std_init

    def store(self, state, action, reward, value, log_prob):
        self.memory.store(state, action, reward, value, log_prob)
        
    def act(self, state):
        state = torch.FloatTensor(state).to(device)
        action, log_prob = self.actor.get_action(state)
        value = self.critic(state)
        return action.detach().cpu().numpy(), value.item(), log_prob.item()
    
    def learn(self):
        if len(self.memory.states) < self.memory.batch_size:
            return

        states = torch.FloatTensor(np.array(self.memory.states)).to(device)
        actions = torch.FloatTensor(np.array(self.memory.actions)).to(device)
        rewards = torch.FloatTensor(np.array(self.memory.rewards)).to(device)
        values = torch.FloatTensor(np.array(self.memory.values)).to(device)
        old_log_probs = torch.FloatTensor(np.array(self.memory.log_probs)).to(device)

        # Normalize rewards
        rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-8)
        
        # Calculate advantages
        advantages = torch.zeros_like(rewards).to(device)
        last_gae = 0
        for t in reversed(range(len(rewards))):
            if t == len(rewards) - 1:
                next_value = 0
            else:
                next_value = values[t + 1]
            delta = rewards[t] + self.gamma * next_value - values[t]
            last_gae = delta + self.gamma * self.gae_lambda * last_gae
            advantages[t] = last_gae
            
        # Normalize advantages
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
        
        for _ in range(self.n_epochs):
            # Generate random mini-batches
            batch_indices = np.random.permutation(len(states))
            
            for start_idx in range(0, len(states), self.memory.batch_size):
                end_idx = start_idx + self.memory.batch_size
                batch_indices_subset = batch_indices[start_idx:end_idx]
                
                states_batch = states[batch_indices_subset]
                actions_batch = actions[batch_indices_subset]
                advantages_batch = advantages[batch_indices_subset]
                old_log_probs_batch = old_log_probs[batch_indices_subset]
                
                # Get current policy outputs
                mean, std = self.actor(states_batch)
                dist = Normal(mean, std)
                new_log_probs = dist.log_prob(actions_batch).sum(dim=-1)
                entropy = dist.entropy().mean()
                
                # Get current value estimate
                current_values = self.critic(states_batch).squeeze()
                
                # Calculate ratios and surrogate objectives
                ratios = torch.exp(new_log_probs - old_log_probs_batch)
                surr1 = ratios * advantages_batch
                surr2 = torch.clamp(ratios, 1-self.clip_epsilon, 1+self.clip_epsilon) * advantages_batch
                
                # Calculate losses with clipped value loss
                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = F.smooth_l1_loss(current_values, rewards[batch_indices_subset])
                entropy_loss = -self.c2 * entropy
                
                total_loss = actor_loss + self.c1 * critic_loss + entropy_loss
                
                # Update networks
                self.actor_optimizer.zero_grad()
                self.critic_optimizer.zero_grad()
                total_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 0.5)
                torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 0.5)
                self.actor_optimizer.step()
                self.critic_optimizer.step()
        
        self.memory.clear()

def ppo_inversion(agent, G, T, alpha, z_dim=100, max_episodes=40000, max_step=5, label=0, model_name="VGG16"):
    print("Target Label : " + str(label))
    best_score = 0
    
    for i_episode in range(1, max_episodes + 1):
        y = torch.tensor([label]).cuda()
        
        # Initialize the state
        z = torch.randn(1, z_dim).cuda()
        state = z.cpu().numpy()[0]
        episode_rewards = []
        
        for t in range(max_step):
            # Get action from PPO agent
            action, value, log_prob = agent.act(state)
            action = torch.FloatTensor(action).cuda()
            
            # Update state and generate images
            z = alpha * z + (1 - alpha) * action.reshape(1, -1)
            next_state = z.cpu().numpy()[0]
            state_image = G(z).detach()
            action_image = G(action.reshape(1, -1)).detach()
            
            # Calculate reward
            _, state_output = T(state_image)
            _, action_output = T(action_image)
            score1 = float(torch.mean(torch.diag(torch.index_select(torch.log(F.softmax(state_output, dim=-1)).data, 1, y))))
            score2 = float(torch.mean(torch.diag(torch.index_select(torch.log(F.softmax(action_output, dim=-1)).data, 1, y))))
            score3 = math.log(max(1e-7, float(torch.index_select(F.softmax(state_output, dim=-1).data, 1, y)) - 
                                float(torch.max(torch.cat((F.softmax(state_output, dim=-1)[0,:y],
                                                         F.softmax(state_output, dim=-1)[0,y+1:])), dim=-1)[0])))
            reward = 2 * score1 + 2 * score2 + 8 * score3
            
            # Store transition
            agent.store(state, action.cpu().numpy(), reward, value, log_prob)
            state = next_state
            episode_rewards.append(reward)
        
        # Learn after collecting multiple steps
        agent.learn()
        
        # Evaluate current policy
        if i_episode % 100 == 0 or i_episode == max_episodes:
            test_images = []
            test_scores = []
            for _ in range(1):
                with torch.no_grad():
                    z_test = torch.randn(1, z_dim).cuda()
                    for t in range(max_step):
                        state_test = z_test.cpu().numpy()[0]
                        action_test, _, _ = agent.act(state_test)
                        action_test = torch.FloatTensor(action_test).cuda()
                        z_test = alpha * z_test + (1 - alpha) * action_test.reshape(1, -1)
                    test_image = G(z_test).detach()
                    test_images.append(test_image.cpu())
                    _, test_output = T(test_image)
                    test_score = float(torch.mean(torch.diag(torch.index_select(F.softmax(test_output, dim=-1).data, 1, y))))
                test_scores.append(test_score)
            
            mean_score = sum(test_scores) / len(test_scores)
            if mean_score >= best_score:
                best_score = mean_score
                best_images = torch.vstack(test_images)
                os.makedirs("./result/images/{}".format(model_name), exist_ok=True)
                os.makedirs("./result/models/{}".format(model_name), exist_ok=True)
                save_image(best_images, "./result/images/{}/{}_{}.png".format(model_name, label, alpha), nrow=10)
                torch.save(agent.actor.state_dict(), "./result/models/{}/actor_{}_{}.pt".format(model_name, label, alpha))
            
            print('Episodes {}/{}, Confidence score: {:.4f}, Best score: {:.4f}'.format(
                i_episode, max_episodes, mean_score, best_score))
    
    return best_images 