"""M4 (опционально): QLoRA-дообучение 7–8B модели на RTX 4090 (≈ 14–18 ГБ VRAM).

    pip install -r requirements-llm.txt
    python -m ml.m4_llm.train_qlora --model Qwen/Qwen2.5-7B-Instruct --data data/sft/train.jsonl \
                                    --out models/llm/qwen7b-explainer --epochs 2

После обучения адаптер подключается к vLLM:
    vllm serve Qwen/Qwen2.5-7B-Instruct --enable-lora --lora-modules explainer=models/llm/qwen7b-explainer \
               --max-model-len 4096 --gpu-memory-utilization 0.85 --port 8001
    FM_LLM_URL=http://localhost:8001/v1  FM_LLM_MODEL=explainer  uvicorn api.main:app

ВНИМАНИЕ: скрипт не прогонялся в этом репозитории (нет GPU с 20+ ГБ). API trl/peft меняется между
версиями — версии закреплены в requirements-llm.txt.
"""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--data", default="data/sft/train.jsonl")
    ap.add_argument("--out", default="models/llm/qwen7b-explainer")
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--grad-acc", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=2048)
    a = ap.parse_args()

    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    tok = AutoTokenizer.from_pretrained(a.model)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(a.model, quantization_config=bnb, device_map="auto",
                                                 torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    ds = load_dataset("json", data_files=a.data, split="train").train_test_split(test_size=0.05, seed=0)
    lora = LoraConfig(r=a.rank, lora_alpha=2 * a.rank, lora_dropout=0.05, task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
    cfg = SFTConfig(output_dir=a.out, num_train_epochs=a.epochs, learning_rate=a.lr, per_device_train_batch_size=a.batch,
                    gradient_accumulation_steps=a.grad_acc, bf16=True, logging_steps=10, eval_strategy="steps",
                    eval_steps=100, save_steps=200, save_total_limit=2, lr_scheduler_type="cosine", warmup_ratio=0.03,
                    max_length=a.max_len, gradient_checkpointing=True, assistant_only_loss=False, report_to="none")
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds["train"], eval_dataset=ds["test"],
                         processing_class=tok, peft_config=lora)
    trainer.train()
    trainer.save_model(a.out)
    tok.save_pretrained(a.out)
    print("адаптер сохранён:", a.out)


if __name__ == "__main__":
    main()
