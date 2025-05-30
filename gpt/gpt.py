import sys
sys.path.append('/home/kisobe/sgoinfre/pip')

from os import path
import torch
import torch.nn as nn
import torch.nn.functional as F
import requests

class FeedForward(nn.Module):
    def __init__(self, emb_dim, dropout):
        super().__init__()
        self.feedforward = nn.Sequential(
            nn.Linear(emb_dim, 4 * emb_dim),
            nn.ReLU(),
            nn.Linear(4 * emb_dim, emb_dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.feedforward(x)
        

class Head(nn.Module):
    """
    Scaled Dot-Product Attention
    see https://arxiv.org/pdf/1706.03762
    """
    def __init__(self, head_size, emb_dim, n_blocks, dropout):
        super().__init__()
        self.key = nn.Linear(emb_dim, head_size, bias=False)
        self.query = nn.Linear(emb_dim, head_size, bias=False)
        self.value = nn.Linear(emb_dim, head_size, bias=False)
        self.register_buffer("tril", torch.tril(torch.ones(n_blocks, n_blocks)))
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        """
        x.shape: (Batch, Time, emb_dim)
        """
        B, T, C = x.shape
        key = self.key(x) # (Batch, Time, head_size)
        query = self.query(x) # (Batch, Time, head_size)
        w = query @ key.transpose(-2, -1) * C**-0.5 # (Batch, Time, Time)
        w = w.masked_fill(self.tril[:T, :T] == 0, float("-inf"))
        w = F.softmax(w, dim=-1) # (Batch, Time, Time)
        w = self.dropout(w)
        value = self.value(x) # (Batch, Time, head_size)
        attention = w @ value # (Batch, Time, head_size)
        return attention


class MultiHeadAttention(nn.Module):
    def __init__(self, n_heads, head_size, emb_dim, n_blocks, dropout):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size, emb_dim, n_blocks, dropout) for _ in range(n_heads)])
        self.proj = nn.Linear(emb_dim, emb_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([head(x) for head in self.heads], dim=-1)
        return self.dropout(self.proj(out))


class Block(nn.Module):
    def __init__(self, n_heads, emb_dim, n_blocks, dropout):
        super().__init__()
        self.multi_head_attention = MultiHeadAttention(n_heads, emb_dim//n_heads, emb_dim, n_blocks, dropout)
        self.feedforward = FeedForward(emb_dim, dropout)
        self.ln1 = nn.LayerNorm(emb_dim)
        self.ln2 = nn.LayerNorm(emb_dim)

    def forward(self, x):
        """
        'x + ': residual connection
        """
        x = x + self.multi_head_attention(self.ln1(x))
        x = x + self.feedforward(self.ln2(x))
        return x
        
    
class BigramLanguageModel(nn.Module):
    def __init__(self, vocab_size, emb_dim, n_blocks, n_heads, dropout):
        super().__init__()
        self.token_embedding = nn.Embedding(vocab_size, emb_dim)
        self.position_embedding = nn.Embedding(n_blocks, emb_dim)
        self.blocks = nn.Sequential(
            Block(n_heads, emb_dim, n_blocks, dropout),
            Block(n_heads, emb_dim, n_blocks, dropout),
            Block(n_heads, emb_dim, n_blocks, dropout),
            Block(n_heads, emb_dim, n_blocks, dropout),
            Block(n_heads, emb_dim, n_blocks, dropout),
            Block(n_heads, emb_dim, n_blocks, dropout),
            nn.LayerNorm(emb_dim)
        )
        self.linear = nn.Linear(emb_dim, vocab_size)

    def forward(self, x, y=None):
        """
        x.shape: (Batch, Time)
        """
        B, T = x.shape
        token_emb = self.token_embedding(x) # (Batch, Time, Channel)
        position_emb = self.position_embedding(torch.arange(0, T)) # (Time, Channel)
        x = token_emb + position_emb
        x = self.blocks(x) # (Batch, Time, head_size)
        logits = self.linear(x) # (Batch, Time, vocab_size)
        if y is None:
            loss = None
        else:
            B, T, C = logits.shape
            logits = logits.view(B * T, -1)
            y = y.view(-1)
            loss = F.cross_entropy(logits, y)
        return logits, loss

    def generate(self, x, iteration, n_blocks, decoder):
        for _ in range(iteration):
            sub_x = x[:, -n_blocks:]
            logits, loss = self(sub_x)
            logits = logits[:, -1, :]
            probs = F.softmax(logits, dim=1)
            x_next = torch.multinomial(probs, 1)
            print(decoder(x_next[0].tolist())[0], end='', flush=True)
            x = torch.cat((x, x_next), dim=1)
        return x
        

if __name__ == "__main__":

    # download data if you need
    input_file_path = path.dirname(__file__) + "/input.txt"
    if not path.exists(input_file_path):
        url = 'https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt'
        r = requests.get(url)
        with open(input_file_path, "w") as f:
            f.write(r.text)

    # get and split dataset
    with open(input_file_path, "r") as f:
        data = f.read()
    chars = sorted(list(set(data)))
    vocab_size = len(chars)
    stoi = { s:i for i, s in enumerate(chars) }
    itos = { i:s for i, s in enumerate(chars) }
    encoder = lambda chars: [stoi[c] for c in chars]
    decoder = lambda ints: [itos[i] for i in ints]
    data = torch.tensor(encoder(data))
    trset_len = int(len(data) * 0.9) # training set: 90%, validation set: 10%
    train_data = data[:trset_len]
    val_data = data[trset_len:]

    def get_batch(split):
        data = train_data if split == "train" else val_data
        indices = torch.randint(0, len(data) - n_blocks, (batch_size,))
        X = torch.stack([data[i:i+n_blocks] for i in indices])
        Y = torch.stack([data[i+1:i+n_blocks+1] for i in indices])
        return X, Y

    # hyperparameters
    epochs = 1000
    eval_interval = 100
    eval_iteration = 100
    batch_size = 64
    n_blocks = 256
    emb_dim = 384
    n_heads = 6
    learning_rate = 3e-4
    dropout = 0.2
    #device = "cuda" if torch.cuda.is_available() else "cpu"

    @torch.no_grad()
    def estimate_loss():
        out = {}
        model.eval()
        for split in ["train", "val"]:
            losses = torch.zeros(eval_iteration)
            for i in range(eval_iteration):
                x, y = get_batch(split)
                logits, loss = model(x, y)
                losses[i] = loss.item()
            out[split] = losses.mean()
        model.train()
        return out
            
    # training and validation
    model = BigramLanguageModel(vocab_size, emb_dim, n_blocks, n_heads, dropout)
    if path.exists("model.pth"):
        model.load_state_dict(torch.load("model.pth", weights_only=True))
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)

    for epoch in range(epochs):
        X, Y = get_batch("train")
        logits, loss = model(X, Y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if epoch % eval_interval == 0:
            estimated_loss = estimate_loss()
            print(f"train loss: {estimated_loss['train']}, validation loss: {estimated_loss['val']}")

    # generate sample texts from trained distribution
    # generated_text = decoder(model.generate(torch.zeros((1, 1), dtype=torch.int32), 1000, n_blocks)[0].tolist())
    # print(''.join(generated_text))

    torch.save(model.state_dict(), "model.pth")
    print("Saved PyTorch Model State to model.pth")
    
