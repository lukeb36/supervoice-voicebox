# Ignore warnings
import warnings
warnings.filterwarnings("ignore")

# Base
import itertools
from glob import glob
import textgrid
from tqdm import tqdm
import time
from contextlib import nullcontext
import shutil
import math
import random

# ML
import torch
import torch.nn.functional as F
from torch.utils.data import DistributedSampler, DataLoader
import pandas
import wandb

# Local
from train_config import config
from voicebox.model_audio import AudioModel
from voicebox.tokenizer import Tokenizer

#
# Device and config
#

project="voicebox_audio"
experiment = "audio"
tags = ["audio", "vctk"]
init_from = "scratch" # or "scratch" or "resume"
train_batch_size = 16
train_steps = 600000
loader_workers = 4
summary_interval = 100
save_interval = 10000
initial_lr = 0.000001
default_lr = 0.00001
warmup_steps = 50000
device = 'cuda:0'
device_type = 'cuda' if 'cuda' in device else 'cpu'
enable_autocast = False
enable_compile = False

#
# Precision
# 

dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float32' # Using float32 since float16 sometimes not that stable
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
autocast = nullcontext() if device_type == 'cpu' or not enable_autocast else torch.amp.autocast(device_type=device_type, dtype=ptdtype)
torch.set_float32_matmul_precision('high')

#
# Resuming
#

checkpoint = None
if init_from == "resume":
    checkpoint = torch.load(f'./checkpoints/{experiment}.pt')
    config = checkpoint['config'] # Reload config

#
# Logger
#

wandb.init(project=project, config=config, tags=tags)

#
# Dataset
#

print("Loading dataset...")

# Load index
files = glob("datasets/vctk-aligned/**/*.TextGrid")
files = [f[len("datasets/vctk-aligned/"):-len(".TextGrid")] for f in files]

# Load textgrids
textgrid = [textgrid.TextGrid.fromFile("datasets/vctk-aligned/" + f + ".TextGrid") for f in files]

# Load audio
files = ["datasets/vctk-prepared/" + f + ".pt" for f in files]

# Tokenizer
tokenizer = Tokenizer()

# Data extractor
def extract_textgrid(src):

    # Prepare
    token_duration = 0.01
    tokens = src[1]
    time = 0
    output_tokens = []
    output_durations = []

    # Iterate over tokens
    for t in tokens:

        # Resolve durations
        ends = t.maxTime
        duration = math.floor((ends - time) / token_duration)
        time = ends

        # Resolve token
        tok = t.mark
        if tok == '':
            tok = tokenizer.silence_token

        # Apply
        output_tokens.append(tok)
        output_durations.append(duration)

    # Outputs
    return output_tokens, output_durations

class TextGridDataset(torch.utils.data.Dataset):
    def __init__(self, textgrid, files):
        self.files = files
        self.textgrid = textgrid
    def __len__(self):
        return len(self.files)        
    def __getitem__(self, index):

        # Load textgrid and audio
        tokens, durations = extract_textgrid(self.textgrid[index])
        audio = torch.load(self.files[index])
        
        # Reshape audio (C, T) -> (T, C)
        audio = audio.transpose(0, 1)

        # Phonemes
        phonemes = []
        for t in range(len(tokens)):
            tok = tokens[t]
            for i in range(durations[t]):
                phonemes.append(tok)
        phonemes = tokenizer(phonemes)

        # Mask
        if random.uniform(0, 1) < 0.3: # If need to mask

            # How much to mask
            mask_len = random.uniform(0.7, 1)

            # Where to mask
            mask_offset = random.uniform(0, 1 - mask_len)

            # Create mask
            mask = torch.zeros(len(phonemes))
            mask_start = math.floor(mask_offset * len(phonemes))
            mask_end = math.floor((mask_offset + mask_len) * len(phonemes))
            mask[mask_start : mask_end] = 1
            mask = mask.bool()
        else:
            mask = torch.ones(len(phonemes)).bool()

        # Cut Audio
        audio = audio[:len(phonemes)]

        # Outputs
        return phonemes, audio, mask

#
# Dataset
#

training_dataset = TextGridDataset(textgrid, files)

#
# Loader
#

def collate_to_shortest(batch):

    # Find minimum length
    min_len = min([b[0].shape[0] for b in batch])

    # Pad
    padded = []
    for b in batch:
        if b[0].shape[0] > min_len:
            offset = random.randint(0, b[0].shape[0] - min_len)
            padded.append((
                b[0][offset:offset + min_len],
                b[1][offset:offset + min_len],
                b[2][offset:offset + min_len],
            ))
        else:
            padded.append((
                b[0],
                b[1],
                b[2],
            ))
    return torch.stack([b[0] for b in padded]), torch.stack([b[1] for b in padded]), torch.stack([b[2] for b in padded])

train_loader = DataLoader(training_dataset, num_workers=loader_workers, shuffle=True, batch_size=train_batch_size, pin_memory=True, collate_fn=collate_to_shortest)

#
# Model
#

step = 0
base_model = AudioModel(tokenizer.n_tokens).to(device)
model = base_model
if enable_compile:
    model = torch.compile(base_model)
    

#
# Optimizer
#

optim = torch.optim.AdamW(base_model.parameters(), initial_lr, betas=[0.8, 0.99])
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optim, T_max = train_steps)

#
# Save/Load
#

def save():
    global epoch
    torch.save({

        # Model
        'model': base_model.state_dict(), 

         # Optimizer
         'optimizer': optim.state_dict(), 
         'scheduler': scheduler.state_dict(),
         'step': step 

    },  f'./checkpoints/{experiment}.pt')
    shutil.copyfile(f'./checkpoints/{experiment}.pt', f'./checkpoints/{experiment}_step_{step}.pt')

if checkpoint is not None:

    # Model
    base_model.load_state_dict(checkpoint['model'])

    # Optimizer
    optim.load_state_dict(checkpoint['optimizer'])
    scheduler.load_state_dict(checkpoint['scheduler'])
    step = checkpoint['step']

    print(f'Loaded at #{epoch}/{step}')

#
# Training
#

def cycle(dl):
    while True:
        for data in dl:
            yield data

loader_cycle = cycle(train_loader)

def train_step():
    global step

    # Update LR
    if step < warmup_steps:
        lr = initial_lr + ((default_lr - initial_lr) * step) / warmup_steps
        for param_group in optim.param_groups:
            param_group['lr'] = lr
    else:
        scheduler.step()
        lr = scheduler.get_last_lr()[0]

    # Load batch
    batch = next(loader_cycle)
    tokens, audio, mask = batch
    tokens = tokens.to(device, non_blocking=True)
    audio = audio.to(device, non_blocking=True)
    mask = mask.to(device, non_blocking=True)

    # Run predictor
    optim.zero_grad()
    predicted, loss = model(tokens, audio, mask, target = audio)

    # Clip gradients
    torch.nn.utils.clip_grad_norm_(model.parameters(), 0.5)

    # Backprop
    loss.backward()
    optim.step()

    # Advance
    step = step + 1

    # Summary
    if step % summary_interval == 0:
        wandb.log({
            "learning_rate": lr,
            "loss": loss,
            "predicted/mean": predicted.mean(),
            "predicted/max": predicted.max(),
            "predicted/min": predicted.min(),
            "target/mean": audio.mean(),
            "target/max": audio.max(),
            "target/min": audio.min(),
            "data/length": mask.shape[1],
        }, step=step)
        
    # Save
    if step % save_interval == 0:
        save()

#
# Start Training
#

print(f'Training {experiment} on {device} with {dtype} precision')
while step < train_steps:
    train_step()