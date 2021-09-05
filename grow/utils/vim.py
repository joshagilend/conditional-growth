import torch
from torch import nn
from torch.optim import SGD


class VariationInformationMaximization:
    def __init__(
        self,
        state_dimensions,
        num_actions,
        num_action_steps,
        null_action,
        num_neurons=256,
        beta=1,
    ):
        self.state_dimensions = state_dimensions
        self.num_action_steps = num_action_steps
        self.null_action = null_action
        self.num_actions = num_actions

        self.action_decoder = TwoLayer(
            self.num_actions + 2 * state_dimensions, num_actions, num_neurons
        )
        self.source_action_state = TwoLayer(
            self.num_actions + state_dimensions, num_actions, num_neurons
        )
        self.source_state = ScalarTwoLayer(self.state_dimensions, 1, num_neurons)

        # Loss to be defined in run.
        self.action_decoder_loss = nn.CrossEntropyLoss()
        self.action_decoder_optimizer = SGD(self.action_decoder.parameters(), lr=0.001)

        self.source_loss = nn.MSELoss()
        self.source_optimizer = SGD(
            list(self.source_action_state.parameters())
            + list(self.source_state.parameters()),
            lr=0.001,
        )

    def get_feature_tensor(self, actions, start_state, end_state=None):
        if end_state is not None:
            num_states = 2
        else:
            num_states = 1
        X = torch.zeros(
            (
                start_state.shape[0],
                self.num_actions + num_states * self.state_dimensions,
            )
        )
        X[:, self.num_actions : self.num_actions + self.state_dimensions] = start_state
        if end_state is not None:
            X[:, self.num_actions + self.state_dimensions :] = end_state

        # Actions are (batch_size, 1)
        actions = torch.squeeze(actions.int(), dim=1)
        for i in range(X.shape[0]):
            X[i, actions[i]] = torch.ones(X.shape[0])
        return X

    def get_action_decoder_probabilities(self, start_state, end_state):
        n = start_state.shape[0]
        probabilities = torch.ones((n, self.num_actions, self.num_action_steps + 1))
        selected_actions = torch.full(
            (n, 1, self.num_action_steps + 1), self.null_action
        )
        for i in range(1, self.num_action_steps):
            X = self.get_feature_tensor(
                selected_actions[..., i - 1], start_state, end_state
            )
            probabilities[..., i] = probabilities[
                ..., i - 1
            ].clone() * self.action_decoder(X)
            selected_actions[..., i] = torch.argmax(probabilities[..., i], dim=1)

            # Drop the identity starting actions.
            return probabilities[..., 1:]

    def get_source_action_state_probabilities(self, start_state):
        n = start_state.shape[0]
        probabilities = torch.ones((n, self.num_actions, self.num_action_steps + 1))
        selected_actions = torch.full(
            (n, 1, self.num_action_steps + 1), self.null_action
        )
        for i in range(1, self.num_action_steps):
            X = self.get_feature_tensor(selected_actions[..., i - 1], start_state)
            probabilities[..., i] = probabilities[
                ..., i - 1
            ].clone() * self.source_action_state(X)
            selected_actions[..., i] = torch.argmax(probabilities[..., i], dim=1)

        return probabilities[..., 1:]

    def step(self, start_state, end_state, actions, temperature):
        """Take a step toward optimizing the ELBO.

        Note:
            States are expected to be pre-processed from x to z via 3D convolution.
            Failing to do so may make optimization much more difficult.

        Parameters:
            start_state: (batch_size, z_start)
            end_state: (batch_size, z_end)
        """

        self.beta = 1 / temperature

        # Sample the approximation of the source distribution.
        source_action_state_probability = self.get_source_action_state_probabilities(
            start_state
        )
        # Maximize the log likelihood of the decoder by minimizing
        # the cross entropy loss.
        # (batch, num_actions, action_step)
        action_decoder_probability = self.get_action_decoder_probabilities(
            start_state, end_state
        )
        self.action_decoder_optimizer.zero_grad()
        action_decoder_loss = 0
        for i in range(self.num_action_steps):
            action_decoder_loss += self.action_decoder_loss(
                action_decoder_probability[..., i], torch.unsqueeze(actions[i], dim=0)
            )

        action_decoder_loss.backward(retain_graph=True)
        self.action_decoder_optimizer.step()
        # print(f"Action Decoder Loss: {action_decoder_loss}")

        # Minimize the loss of the approximation to the source distribution.
        self.source_optimizer.zero_grad()
        action_decoder_probability = self.get_action_decoder_probabilities(
            start_state, end_state
        )

        x = self.beta * torch.sum(torch.log(action_decoder_probability), dim=2)
        y = torch.sum(
            torch.log(source_action_state_probability), dim=2
        ) + self.source_state(start_state)
        source_loss = self.source_loss(x, y)
        source_loss.backward()
        self.source_optimizer.step()
        # print(f"Source Loss: {source_loss}")
        # print(f"Empowerment: {self.get_empowerment(start_state)}")

        return action_decoder_loss, source_loss, self.get_empowerment(start_state)

    def get_empowerment(self, start_state):
        return 1 / self.beta * self.source_state(start_state).item()


class TwoLayer(nn.Module):
    def __init__(self, num_inputs, num_outputs, num_neurons=256):
        super(TwoLayer, self).__init__()
        self.two_layer = nn.Sequential(
            nn.Linear(num_inputs, num_neurons),
            nn.ReLU(),
            nn.Linear(num_neurons, num_neurons),
            nn.ReLU(),
            nn.Linear(num_neurons, num_outputs),
            nn.Softmax(dim=1),
        )

    def forward(self, x):
        return self.two_layer(x)


class ScalarTwoLayer(nn.Module):
    def __init__(self, num_inputs, num_outputs, num_neurons=256):
        super(ScalarTwoLayer, self).__init__()
        self.two_layer = nn.Sequential(
            nn.Linear(num_inputs, num_neurons),
            nn.ReLU(),
            nn.Linear(num_neurons, num_neurons),
            nn.ReLU(),
            nn.Linear(num_neurons, num_outputs),
        )

    def forward(self, x):
        return self.two_layer(x)
