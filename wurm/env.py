import torch
import torch.nn.functional as F
from typing import List
from time import time

from config import FOOD_CHANNEL, HEAD_CHANNEL, BODY_CHANNEL
from wurm.utils import food, head, body, determine_orientations, drop_duplicates
from wurm._filters import *


class SingleSnakeEnvironments(object):
    """Many environments containing a single snake as a batched image tensor.

    Each environment has 3 channels. Each index has the following meaning
    0 - Food channel. 1 = food, 0 = no food
    1 - Head channel. 1 = head of snake, 0 = empty
    2 - Body channel. Each segment of the snake is represented as a positive integer. 1 is the tail of the
        snake and the maximum number is at the same position as the head of the snake in the head channel.

    Example of a single environment containing a snake of length 8, all unfilled squares represent 0s.

    Food channel                 Head channel                 Body channel
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   |   |   |   |   |   |    |   |   |   |   |   |   |    |   |   |   |   |   |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   |   |   |   |   |   |    |   |   |   |   |   |   |    |   |   | 3 | 4 | 5 |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   |   |   |   |   |   |    |   |   |   |   |   |   |    |   | 1 | 2 |   | 6 |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   |   |   |   |   |   |    |   |   |   |   |   |   |    |   |   |   |   | 7 |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   | 1 |   |   |   |   |    |   |   |   |   | 1 |   |    |   |   |   |   | 8 |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+
    |   |   |   |   |   |   |    |   |   |   |   |   |   |    |   |   |   |   |   |   |
    +---+---+---+---+---+---+    +---+---+---+---+---+---+    +---+---+---+---+---+---+

    The advantage of this representation is that the dynamics of multiple environments can be stepped in a parallel
    fashion using using just tensor operations allowing one to run 1000s of envs in parallel on a single machine.
    """
    def __init__(self,
                 num_envs: int,
                 size: int,
                 max_timesteps: int = None,
                 initial_snake_length: int = 3,
                 on_death: str = 'restart',
                 device: str = DEFAULT_DEVICE,
                 manual_setup: bool = False):
        """Initialise the environments

        Args:
            num_envs:
            size:
            max_timesteps:
        """
        self.num_envs = num_envs
        self.size = size
        self.max_timesteps = max_timesteps
        self.initial_snake_length = initial_snake_length
        self.on_death = on_death
        self.device = device

        self.envs = torch.zeros((num_envs, 3, size, size)).to(self.device)
        self.t = 0

        if not manual_setup:
            # Create environments
            self.envs = self._create_envs(self.num_envs)

    def step(self, actions: torch.Tensor) -> (torch.Tensor, torch.Tensor, torch.Tensor, List[dict]):
        if actions.dtype not in (torch.short, torch.int, torch.long):
            raise TypeError('actions Tensor must be an integer type i.e. '
                            '{torch.ShortTensor, torch.IntTensor, torch.LongTensor}')

        if actions.shape[0] != self.num_envs:
            raise RuntimeError('Must have the same number of actions as environments.')

        reward = torch.zeros((self.num_envs,)).long().to(self.device)
        done = torch.zeros((self.num_envs,)).byte().to(self.device)
        info = [dict(), ] * self.num_envs

        t0 = time()
        snake_sizes = self.envs[:, BODY_CHANNEL:BODY_CHANNEL + 1, :].view(self.num_envs, -1).max(dim=1)[0]

        orientations = determine_orientations(self.envs)
        print(f'Orientations: {time()-t0}s')

        t0 = time()
        # Check if any snakes are trying to move backwards and change
        # their direction/action to just continue forward
        # The test for this is if their orientation number {0, 1, 2, 3}
        # is the same as their action
        mask = orientations == actions
        actions.add_((mask * 2).long()).fmod_(4)

        # Create head position deltas
        head_deltas = F.conv2d(head(self.envs), ORIENTATION_FILTERS.to(self.device), padding=1)
        # Select the head position delta corresponding to the correct action
        actions_onehot = torch.FloatTensor(self.num_envs, 4).to(self.device)
        actions_onehot.zero_()
        actions_onehot.scatter_(1, actions.unsqueeze(-1), 1)
        head_deltas = torch.einsum('bchw,bc->bhw', [head_deltas, actions_onehot]).unsqueeze(1)

        # Move head position by applying delta
        self.envs[:, HEAD_CHANNEL:HEAD_CHANNEL + 1, :, :].add_(head_deltas[:, 0:1, :, :]).round_()
        print(f'Head movement: {time() - t0}s')

        t0 = time()
        # Check for hitting self
        hit_self = (head(self.envs) * body(self.envs)).view(self.num_envs, -1).sum(dim=-1) > 0
        done = torch.clamp(done + hit_self, 0, 1)
        print(f'Self collision: {time() - t0}s')

        ################
        # Apply update #
        ################

        t0 = time()
        # Decay the body sizes by 1, hence moving the body
        body_movement = torch.zeros_like(self.envs)
        body_movement[:, BODY_CHANNEL, :, :] = -1
        self.envs = F.relu(self.envs + body_movement)
        # Create a new head position in the body channel
        self.envs[:, BODY_CHANNEL:BODY_CHANNEL + 1, :, :] += \
            head(self.envs) * snake_sizes[:, None, None, None].expand((self.num_envs, 1, self.size, self.size))
        # Apply food growth i.e. +1 to all snake locations
        self.envs[:, BODY_CHANNEL:BODY_CHANNEL + 1, :, :] += \
            ((head(self.envs) * food(self.envs)).sum() > 0) * body(self.envs).clamp(0, 1)

        # Remove food and give reward
        # `food_removal` is 0 except where a snake head is at the same location as food where it is -1
        food_removal = head(self.envs) * food(self.envs) * -1
        reward.sub_(food_removal.view(self.num_envs, -1).sum(dim=-1).long())
        self.envs[:, FOOD_CHANNEL:FOOD_CHANNEL + 1, :, :] += food_removal
        print(f'Body movement and food removal: {time() - t0}s')

        # Add new food if necessary.
        if food_removal.sum() < 0:
            t0 = time()
            food_addition_env_indices = (food_removal * -1).view(self.num_envs, -1).sum(dim=-1).byte()
            add_food_envs = self.envs[food_addition_env_indices, :, :, :]
            self.envs[food_addition_env_indices, :, :, :].add_(self._get_food_addition(add_food_envs))

            print(f'Food addition to {food_addition_env_indices.sum().item()} envs: {time() - t0}s')

        t0 = time()
        # Check for boundary, Done by performing a convolution with no padding
        # If the head is at the edge then it will be cut off and the sum of the head
        # channel will be 0
        head_at_edge = F.conv2d(
            head(self.envs),
            NO_CHANGE_FILTER.to(self.device),
        ).view(self.num_envs, -1).sum(dim=-1) == 0
        done = torch.clamp(done + head_at_edge, 0, 1)
        print(f'Edge collision: {time() - t0}s')

        return self.envs, reward, done, info

    def _select_from_available_locations(self, locs: torch.Tensor) -> torch.Tensor:
        locations = torch.nonzero(locs)
        random_loc = locations[torch.randperm(locations.shape[0])[:1]]
        return random_loc

    def _get_food_addition(self, envs: torch.Tensor):
        # Get empty locations
        available_locations = envs.sum(dim=1, keepdim=True) == 0
        # Remove boundaries
        available_locations[:, :, :1, :] = 0
        available_locations[:, :, :, :1] = 0
        available_locations[:, :, -1:, :] = 0
        available_locations[:, :, :, -1:] = 0

        food_indices = drop_duplicates(torch.nonzero(available_locations), 0)
        food_addition = torch.sparse_coo_tensor(
            food_indices.t(),  torch.ones(len(food_indices)), available_locations.shape, device=self.device)
        food_addition = food_addition.to_dense()

        return food_addition

    def reset(self, done: torch.Tensor):
        """Resets environments in which the snake has died

        Args:
            done: A 1D Tensor of length self.num_envs. A value of 1 means the corresponding environment needs to be
                reset
        """
        t0 = time()

        if done.sum() > 0:
            new_envs = self._create_envs(done.sum().item())
            self.envs[done, :, :, :] = new_envs

        print(f'Resetting {done.sum().item()} envs: {time() - t0}s')

    def _create_envs(self, num_envs: int):
        """Vectorised environment creation. Creates self.num_envs environments simultaneously."""
        if self.size <= 10:
            raise NotImplementedError('Cannot make an env this small without making this code more clever')

        if self.initial_snake_length != 3:
            raise NotImplementedError('Only initial snake length = 3 has been implemented.')

        envs = torch.zeros((num_envs, 3, self.size, self.size)).to(self.device)

        # Create random locations to seed bodies
        body_seed_indices = torch.stack([
            torch.arange(num_envs),
            torch.zeros((num_envs,)).long(),
            torch.randint(1 + self.initial_snake_length, self.size - (1 + self.initial_snake_length), size=(num_envs,)),
            torch.randint(1 + self.initial_snake_length, self.size - (1 + self.initial_snake_length), size=(num_envs,))
        ]).to(self.device)
        body_seeds = torch.sparse_coo_tensor(
            body_seed_indices, torch.ones(num_envs), (num_envs, 1, self.size, self.size), device=self.device
        )

        # Choose random starting directions
        random_directions = torch.randint(4, (num_envs,)).to(self.device)
        random_directions_onehot = torch.Tensor(num_envs, 4).float().to(self.device)
        random_directions_onehot.zero_()
        random_directions_onehot.scatter_(1, random_directions.unsqueeze(-1), 1)

        # Create bodies
        bodies = torch.einsum('bchw,bc->bhw', [
            F.conv2d(body_seeds.to_dense(), LENGTH_3_SNAKES.to(self.device), padding=1),
            random_directions_onehot
        ]).unsqueeze(1)
        envs[:, BODY_CHANNEL:BODY_CHANNEL+1, :, :] = bodies

        # Create heads at end of bodies
        snake_sizes = envs[:, BODY_CHANNEL:BODY_CHANNEL + 1, :].view(num_envs, -1).max(dim=1)[0]
        snake_size_mask = snake_sizes[:, None, None, None].expand((num_envs, 1, self.size, self.size))
        envs[:, HEAD_CHANNEL:HEAD_CHANNEL + 1, :, :] = (bodies == snake_size_mask).float()

        # Add food
        available_food_locations = envs.sum(dim=1, keepdim=True) == 0
        # Remove boundaries
        available_food_locations[:, :, :1, :] = 0
        available_food_locations[:, :, :, :1] = 0
        available_food_locations[:, :, -1:, :] = 0
        available_food_locations[:, :, :, -1:] = 0

        food_indices = drop_duplicates(torch.nonzero(available_food_locations), 0)
        food_addition = torch.sparse_coo_tensor(
            food_indices.t(), torch.ones(len(food_indices)), available_food_locations.shape, device=self.device)
        envs[:, FOOD_CHANNEL:FOOD_CHANNEL + 1, :, :] = food_addition.to_dense()

        return envs
