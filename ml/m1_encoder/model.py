"""M1 Vydel Encoder — трансформер над множеством токенов «порода × ярус» + общий токен выдела."""
from __future__ import annotations

import torch
from torch import nn

from .tokens import G_CAT, G_NUM, TOK_NUM


class VydelEncoder(nn.Module):
    def __init__(self, vocab_sizes: dict[str, int], d_model: int = 256, n_layers: int = 6, n_heads: int = 8,
                 ff: int = 1024, emb_dim: int = 128, dropout: float = 0.1):
        super().__init__()
        d = d_model
        self.sp = nn.Embedding(vocab_sizes["species"], d)
        self.layer = nn.Embedding(vocab_sizes["layer"], d)
        self.dom = nn.Embedding(3, d)
        self.tok_num = nn.Linear(2 * len(TOK_NUM), d)
        self.g_cat = nn.ModuleList([nn.Embedding(vocab_sizes[k], d) for k in G_CAT])
        self.g_num = nn.Linear(2 * len(G_NUM), d)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        enc = nn.TransformerEncoderLayer(d, n_heads, ff, dropout, batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(enc, n_layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(d)
        self.proj = nn.Sequential(nn.Linear(d, d), nn.GELU(), nn.Linear(d, emb_dim))
        # головы предобучения (masked modeling)
        self.head_tok_num = nn.Linear(d, len(TOK_NUM))
        self.head_species = nn.Linear(d, vocab_sizes["species"])
        self.head_g_num = nn.Linear(d, len(G_NUM))

    def encode(self, tok_cat, tok_num, tok_nmask, tok_valid, g_cat, g_num, g_nmask):
        x = self.sp(tok_cat[..., 0]) + self.layer(tok_cat[..., 1]) + self.dom(tok_cat[..., 2])
        x = x + self.tok_num(torch.cat([tok_num * tok_nmask, tok_nmask], -1))
        g = self.g_num(torch.cat([g_num * g_nmask, g_nmask], -1))
        for i, emb in enumerate(self.g_cat):
            g = g + emb(g_cat[:, i])
        g = g.unsqueeze(1) + self.cls
        h = torch.cat([g, x], 1)
        pad = torch.cat([torch.zeros_like(tok_valid[:, :1]), 1 - tok_valid], 1) > 0.5
        h = self.norm(self.encoder(h, src_key_padding_mask=pad))
        return h          # [B, 1+T, d]; h[:,0] — выдел

    def forward(self, tok_cat, tok_num, tok_nmask, tok_valid, g_cat, g_num, g_nmask):
        h = self.encode(tok_cat, tok_num, tok_nmask, tok_valid, g_cat, g_num, g_nmask)
        return {"h": h, "tok_num": self.head_tok_num(h[:, 1:]), "species": self.head_species(h[:, 1:]),
                "g_num": self.head_g_num(h[:, 0]), "emb": nn.functional.normalize(self.proj(h[:, 0]), dim=-1)}


class EmbeddingOnly(nn.Module):
    """Обёртка для ONNX: только эмбеддинг выдела."""

    def __init__(self, m: VydelEncoder):
        super().__init__()
        self.m = m

    def forward(self, tok_cat, tok_num, tok_nmask, tok_valid, g_cat, g_num, g_nmask):
        h = self.m.encode(tok_cat, tok_num, tok_nmask, tok_valid, g_cat, g_num, g_nmask)
        return nn.functional.normalize(self.m.proj(h[:, 0]), dim=-1)
