import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import utils
import numpy as np

class PPO(object):

    def set_this_layer(self, this_layer):
        self.this_layer = this_layer
        self.init_actor_critic()

    def init_actor_critic(self):
        self.optimizer_actor_critic = optim.Adam(self.this_layer.actor_critic.parameters(), lr=self.this_layer.args.lr, eps=self.this_layer.args.eps)
        self.one = torch.FloatTensor([1]).cuda()
        self.mone = self.one * -1

        self.actor_critic_gradients_reward = False
        self.actor_critic_preserve_prediction = False
        self.transition_model_gradients_reward = False
        self.transition_model_preserve_prediction = False

        if self.this_layer.hierarchy_id != (self.this_layer.args.num_hierarchy-1):

            if self.this_layer.args.encourage_ac_connection in ['actor_critic','both']:

                if self.this_layer.args.encourage_ac_connection_type in ['gradients_reward']:
                    self.actor_critic_gradients_reward = True

                if self.this_layer.args.encourage_ac_connection_type in ['preserve_prediction']:
                    self.actor_critic_preserve_prediction = True

            if self.this_layer.args.encourage_ac_connection in ['transition_model','both']:

                if self.this_layer.args.encourage_ac_connection_type in ['gradients_reward']:
                    self.transition_model_gradients_reward = True

                if self.this_layer.args.encourage_ac_connection_type in ['preserve_prediction']:
                    self.transition_model_preserve_prediction = True

        if self.actor_critic_preserve_prediction:

            self.action_onehot_travel_batch = torch.zeros(self.this_layer.args.actor_critic_mini_batch_size*self.this_layer.action_space.n,self.this_layer.action_space.n).cuda()
            batch_i = 0
            for action_i in range(self.this_layer.action_space.n):
                for mini_batch_i in range(self.this_layer.args.actor_critic_mini_batch_size):
                    self.action_onehot_travel_batch[batch_i][action_i] = 1.0
                    batch_i += 1

            self.batch_index_travel = np.array(range(self.this_layer.args.actor_critic_mini_batch_size*self.this_layer.action_space.n))

    def set_upper_layer(self, upper_layer):
        '''this method will be called if we have a transition_model to generate reward bounty'''
        self.upper_layer = upper_layer
        if self.upper_layer.transition_model is not None:
            self.init_transition_model()

    def init_transition_model(self):
        '''build essential things for training transition_model'''
        self.optimizer_transition_model = optim.Adam(self.upper_layer.transition_model.parameters(), lr=1e-4, betas=(0.0, 0.9))
        if self.this_layer.args.mutual_information:
            self.NLLLoss = nn.NLLLoss()

    def get_grad_norm(self, inputs, outputs):

        gradients = torch.autograd.grad(
            outputs=outputs,
            inputs=inputs,
            grad_outputs=torch.ones(outputs.size()).cuda(),
            create_graph=True,
            retain_graph=True,
            only_inputs=True
        )[0]
        gradients = gradients.contiguous()
        gradients_fl = gradients.view(gradients.size()[0],-1)
        gradients_norm = gradients_fl.norm(2, dim=1) / ((gradients_fl.size()[1])**0.5)

        return gradients_norm

    def update(self, update_type):

        epoch_loss = {}

        '''train actor_critic'''
        if update_type in ['actor_critic','both']:

            '''compute advantages'''
            advantages = self.this_layer.rollouts.returns[:-1] - self.this_layer.rollouts.value_preds[:-1]
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-5)

            '''prepare epoch'''
            epoch = self.this_layer.args.actor_critic_epoch
            if (self.this_layer.update_i in [0,1]) and (not self.this_layer.checkpoint_loaded):
                print('[H-{}] {}-th time train actor_critic, skip, since transition_model need to be trained first.'.format(
                    self.this_layer.hierarchy_id,
                    self.this_layer.update_i,
                ))
                epoch *= 0

            for e in range(epoch):

                data_generator = self.this_layer.rollouts.feed_forward_generator(
                    advantages = advantages,
                    mini_batch_size = self.this_layer.args.actor_critic_mini_batch_size,
                )

                for sample in data_generator:

                    self.optimizer_actor_critic.zero_grad()

                    observations_batch, input_actions_batch, states_batch, actions_batch, \
                       return_batch, masks_batch, old_action_log_probs_batch, \
                            adv_targ = sample

                    input_actions_index = input_actions_batch.nonzero()[:,1]

                    if self.actor_critic_gradients_reward:

                        input_actions_batch = torch.autograd.Variable(input_actions_batch, requires_grad=True)

                    if self.actor_critic_preserve_prediction:

                        '''repeat inputs so that every action has a simple'''
                        observations_batch = observations_batch.repeat(self.this_layer.action_space.n,1,1,1)
                        states_batch       = states_batch      .repeat(self.this_layer.action_space.n,1)
                        masks_batch        = masks_batch       .repeat(self.this_layer.action_space.n,1)
                        actions_batch      = actions_batch     .repeat(self.this_layer.action_space.n,1)

                        '''after repeat, it will be s0, s0, s1, s1, s2, s2...'''

                        input_actions_batch = self.action_onehot_travel_batch

                    # Reshape to do in a single forward pass for all steps
                    values, action_log_probs, dist_entropy, _, dist_features = self.this_layer.actor_critic.evaluate_actions(
                        inputs       = observations_batch,
                        states       = states_batch,
                        masks        = masks_batch,
                        action       = actions_batch,
                        input_action = input_actions_batch,
                    )

                    if self.actor_critic_preserve_prediction:

                        input_actions_index_for_acted_actions = (input_actions_index*self.this_layer.args.actor_critic_mini_batch_size+torch.LongTensor(range(self.this_layer.args.actor_critic_mini_batch_size)).long().cuda())

                        self.input_actions_index_for_preserved_actions = torch.LongTensor(
                            np.delete(
                                arr = self.batch_index_travel,
                                obj = input_actions_index_for_acted_actions.cpu().numpy(),
                                axis = 0,
                            )
                        ).cuda()

                        def select_preserved(x):
                            return torch.index_select(x, 0, self.input_actions_index_for_preserved_actions).detach()

                        self.preserve_values           = select_preserved(values)
                        self.preserve_dist_features = select_preserved(dist_features)

                        def select_acted(x):
                            return torch.index_select(x, 0, input_actions_index_for_acted_actions)

                        values           = select_acted(values)
                        action_log_probs = select_acted(action_log_probs)
                        dist_entropy     = select_acted(dist_entropy)

                    ratio = torch.exp(action_log_probs - old_action_log_probs_batch)
                    surr1 = ratio * adv_targ
                    surr2 = torch.clamp(ratio, 1.0 - self.this_layer.args.clip_param,
                                               1.0 + self.this_layer.args.clip_param) * adv_targ
                    action_loss = -torch.min(surr1, surr2)
                    epoch_loss['action_{}'.format(input_actions_index[0])] = action_loss[0,0].item()
                    action_loss = action_loss.mean()

                    value_loss = (return_batch-values).pow(2) * self.this_layer.args.value_loss_coef
                    epoch_loss['value_{}'.format(input_actions_index[0])] = value_loss[0,0].item()
                    value_loss = value_loss.mean()

                    if self.actor_critic_gradients_reward:
                        dist_entropy_before_mean = dist_entropy

                    dist_entropy = dist_entropy * self.this_layer.args.entropy_coef
                    epoch_loss['dist_entropy_{}'.format(input_actions_index[0])] = dist_entropy[0].item()
                    dist_entropy = dist_entropy.mean()

                    final_loss = value_loss + action_loss - dist_entropy

                    final_loss.backward(
                        retain_graph = (self.this_layer.args.encourage_ac_connection in ['actor_critic','both']),
                    )

                    if self.actor_critic_gradients_reward:

                        gradients_norm = self.get_grad_norm(
                            inputs = input_actions_batch,
                            outputs = dist_entropy_before_mean,
                        )
                        gradients_reward = (gradients_norm+1.0).log()*self.this_layer.args.encourage_ac_connection_coefficient
                        print(gradients_reward.size())
                        print(s)
                        # DEBUG:
                        epoch_loss['gradients_reward_{}'.format(input_actions_index[0])] = gradients_reward[0].item()
                        gradients_reward = gradients_reward.mean()
                        gradients_reward.backward(self.mone)

                    nn.utils.clip_grad_norm_(self.this_layer.actor_critic.parameters(),
                                             self.this_layer.args.max_grad_norm)

                    self.optimizer_actor_critic.step()

                    if self.actor_critic_preserve_prediction:

                        self.optimizer_actor_critic.zero_grad()

                        def select_preserved(x):
                            return torch.index_select(x  , 0, self.input_actions_index_for_preserved_actions)

                        observations_batch = select_preserved(observations_batch)
                        states_batch = select_preserved(states_batch)
                        masks_batch = select_preserved(masks_batch)
                        actions_batch = select_preserved(actions_batch)
                        input_actions_batch = select_preserved(input_actions_batch)

                        values, action_log_probs, dist_entropy, _, dist_features = self.this_layer.actor_critic.evaluate_actions(
                            inputs       = observations_batch,
                            states       = states_batch,
                            masks        = masks_batch,
                            action       = actions_batch,
                            input_action = input_actions_batch,
                        )

                        preserve_prediction_values = F.mse_loss(
                            input = values,
                            target = self.preserve_values,
                        )
                        epoch_loss['preserve_prediction_values'] += preserve_prediction_values.item()

                        preserve_prediction_dist_features = F.mse_loss(
                            input = dist_features,
                            target = self.preserve_dist_features,
                        )
                        epoch_loss['preserve_prediction_dist_features'] += preserve_prediction_dist_features.item()

                        preserve_prediction_loss = (preserve_prediction_values+preserve_prediction_dist_features)*self.this_layer.args.encourage_ac_connection_coefficient
                        epoch_loss['preserve_prediction'] += preserve_prediction_loss.item()

                        preserve_prediction_loss.backward()

                        self.optimizer_actor_critic.step()

        '''train transition_model'''
        if update_type in ['transition_model','both']:

            '''prepare epoch'''
            epoch = self.this_layer.args.transition_model_epoch
            if (self.this_layer.update_i in [0,1]) and (not self.this_layer.checkpoint_loaded):
                print('[H-{}] {}-th time train transition_model, train more epoch'.format(
                    self.this_layer.hierarchy_id,
                    self.this_layer.update_i,
                ))
                epoch = 800

            for e in range(epoch):

                data_generator = self.upper_layer.rollouts.transition_model_feed_forward_generator(
                    mini_batch_size = int(self.this_layer.args.transition_model_mini_batch_size),
                    recent_steps = int(self.this_layer.rollouts.num_steps/self.this_layer.hierarchy_interval)-1,
                    recent_at = self.upper_layer.step_i,
                )

                for sample in data_generator:

                    observations_batch, next_observations_batch, actions_batch, next_masks_batch, reward_bounty_raw_batch = sample

                    self.optimizer_transition_model.zero_grad()

                    action_onehot_batch = torch.zeros(observations_batch.size()[0],self.upper_layer.actor_critic.output_action_space.n).cuda()

                    '''convert actions_batch to action_onehot_batch'''
                    action_onehot_batch.fill_(0.0)
                    action_onehot_batch.scatter_(1,actions_batch.long(),1.0)

                    '''generate indexs'''
                    next_masks_batch_index = next_masks_batch.squeeze().nonzero().squeeze()
                    next_masks_batch_index_observations_batch      = next_masks_batch_index.unsqueeze(1).unsqueeze(2).unsqueeze(3).expand(next_masks_batch_index.size()[0],*observations_batch     .size()[1:])
                    next_masks_batch_index_next_observations_batch = next_masks_batch_index.unsqueeze(1).unsqueeze(2).unsqueeze(3).expand(next_masks_batch_index.size()[0],*next_observations_batch.size()[1:])
                    next_masks_batch_index_action_onehot_batch     = next_masks_batch_index.unsqueeze(1)                          .expand(next_masks_batch_index.size()[0],*action_onehot_batch    .size()[1:])
                    next_masks_batch_index_reward_bounty_raw_batch = next_masks_batch_index.unsqueeze(1)                          .expand(next_masks_batch_index.size()[0],*reward_bounty_raw_batch.size()[1:])
                    '''index mini batch'''
                    observations_batch = observations_batch.gather(0,next_masks_batch_index_observations_batch)
                    action_onehot_batch = action_onehot_batch.gather(0,next_masks_batch_index_action_onehot_batch)
                    next_observations_batch = next_observations_batch.gather(0,next_masks_batch_index_next_observations_batch)
                    reward_bounty_raw_batch = reward_bounty_raw_batch.gather(0,next_masks_batch_index_reward_bounty_raw_batch)
                    if observations_batch.size()[0] < 16:
                        '''at least it should have 16 samples, otherwise the batch normlize is not working properly'''
                        print('# WARNING: skip this batch since the mini_batch_size after indexing is {}'.format(observations_batch.size()[0]))
                        continue

                    if self.this_layer.args.encourage_ac_connection in ['transition_model','both']:
                        action_onehot_batch = torch.autograd.Variable(action_onehot_batch, requires_grad=True)

                    '''forward'''
                    self.upper_layer.transition_model.train()

                    if not self.this_layer.args.mutual_information:
                        predicted_next_observations_batch, reward_bounty = self.upper_layer.transition_model(
                            inputs = observations_batch,
                            input_action = action_onehot_batch,
                        )
                        '''compute mse loss'''
                        mse_loss_transition = F.mse_loss(
                            input = predicted_next_observations_batch,
                            target = next_observations_batch,
                            reduction='elementwise_mean',
                        )/255.0

                    else:
                        predicted_action_log_probs, reward_bounty = self.upper_layer.transition_model(
                            inputs = next_observations_batch,
                        )
                        '''compute mse loss'''
                        nll_loss = self.NLLLoss(predicted_action_log_probs, action_onehot_batch.nonzero()[:,1])

                    mse_loss_reward_bounty = F.mse_loss(
                        input = reward_bounty,
                        target = reward_bounty_raw_batch,
                        reduction='elementwise_mean',
                    )
                    if self.this_layer.update_i in [0]:
                        mse_loss_reward_bounty *= 0.0

                    if not self.this_layer.args.mutual_information:
                        if self.this_layer.update_i not in [0]:
                            mse_loss = mse_loss_transition + mse_loss_reward_bounty
                        else:
                            mse_loss = mse_loss_transition
                    else:
                        if self.this_layer.update_i not in [0]:
                            mse_loss = nll_loss + mse_loss_reward_bounty
                        else:
                            mse_loss = nll_loss

                    '''backward'''
                    mse_loss.backward(
                        retain_graph = (self.this_layer.args.encourage_ac_connection in ['transition_model','both']),
                    )

                    if self.this_layer.args.encourage_ac_connection in ['transition_model','both']:

                        if self.this_layer.args.encourage_ac_connection_type in ['preserve_prediction']:
                            raise Exception('Not implemented yet.')

                        elif self.this_layer.args.encourage_ac_connection_type in ['gradients_reward']:
                            gradients_norm = self.get_grad_norm(
                                inputs = action_onehot_batch,
                                outputs = predicted_next_observations_batch,
                            )
                            gradients_reward = (gradients_norm+1.0).log().mean()*self.this_layer.args.encourage_ac_connection_coefficient
                            gradients_reward.backward(self.mone)

                    self.optimizer_transition_model.step()


                if self.this_layer.update_i in [0,1]:
                    print_str = ''
                    print_str += '[H-{}] {}-th time train transition_model, epoch {}, ml {}, mlrb {}'.format(
                        self.this_layer.hierarchy_id,
                        self.this_layer.update_i,
                        e,
                        mse_loss.item(),
                        mse_loss_reward_bounty.item(),
                    )
                    if not self.this_layer.args.mutual_information:
                        print_str += ', mlt {}'.format(
                            mse_loss_transition.item(),
                        )
                    else:
                        print_str += ', nl {}'.format(
                            nll_loss.item(),
                        )
                    print(print_str)

            if not self.this_layer.args.mutual_information:
                epoch_loss['mse_loss_transition'] = mse_loss.item()
            else:
                epoch_loss['nll_loss'] = nll_loss.item()
            epoch_loss['mse_loss_reward_bounty'] = mse_loss.item()

            if self.this_layer.args.encourage_ac_connection_type in ['gradients_reward']:
                epoch_loss['gradients_reward'] = gradients_reward.item()

        return epoch_loss
