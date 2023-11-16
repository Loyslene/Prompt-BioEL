import argparse
import os
# os.environ['CUDA_VISIBLE_DEVICES'] = "2"
import torch
from transformers import RobertaTokenizer, \
  get_linear_schedule_with_warmup, get_constant_schedule
import numpy as np
from disambiguation import *
from data_disambiguation import *
from utils import *
from loss import *
from datetime import datetime
from torch.optim import AdamW
from tqdm import tqdm
from pretrain import MaskLMEncoder


def set_seeds(args):
  random.seed(args.seed)
  np.random.seed(args.seed)
  torch.manual_seed(args.seed)


def count_parameters(model):
  return sum(p.numel() for p in model.parameters() if p.requires_grad)


def strtime(datetime_checkpoint):
  diff = datetime.now() - datetime_checkpoint
  return str(diff).rsplit('.')[0]  # Ignore below seconds


def get_pretrained_model(pretrained_model, tokenizer, device, args):
  model = MaskLMEncoder(pretrained_model, tokenizer, device)
  state_dict = torch.load(args.pretrained_model_path) if device.type == 'cuda' else \
    torch.load(args.model, map_location=torch.device('cpu'))
  model.load_state_dict(state_dict["sd"], strict=False)
  return model.model.bert


def load_model(is_init, device, type_loss, args):
  model = PromptEncoder(args.pretrained_model, device, type_loss)
  # model = CaEncoder(args.pretrained_model, device, type_loss)
  if args.use_pretrained_model:
    model.model = get_pretrained_model(args.pretrained_model, args.tokenizer, device, args)
  if not is_init:
    state_dict = torch.load(args.model) if device.type == 'cuda' else \
      torch.load(args.model, map_location=torch.device('cpu'))
    model.load_state_dict(state_dict['sd'], strict=False)
  return model


def configure_optimizer(args, model, num_train_examples):
  # https://github.com/google-research/bert/blob/master/optimization.py#L25
  no_decay = ['bias', 'LayerNorm.weight']
  optimizer_grouped_parameters = [
    {'params': [p for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay)],
     'weight_decay': args.weight_decay},
    {'params': [p for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay)],
     'weight_decay': 0.0}
  ]
  optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr,
                    eps=args.adam_epsilon)

  num_train_steps = int(num_train_examples / args.batch /
                        args.gradient_accumulation_steps * args.epochs)
  num_warmup_steps = int(num_train_steps * args.warmup_proportion)

  scheduler = get_linear_schedule_with_warmup(
    optimizer, num_warmup_steps=num_warmup_steps,
    num_training_steps=num_train_steps)

  return optimizer, scheduler, num_train_steps, num_warmup_steps


def configure_optimizer_simple(args, model, num_train_examples):
  optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
  num_train_steps = int(num_train_examples / args.B /
                        args.gradient_accumulation_steps * args.epochs)
  num_warmup_steps = 0

  scheduler = get_constant_schedule(optimizer)

  return optimizer, scheduler, num_train_steps, num_warmup_steps


def get_hit_scores(indices, labels):
  hit = 0
  nums = len(labels)
  for i in range(nums):
    indice = indices[i]
    label = labels[i]
    hit += any([label[index] for index in indice])
  return hit / nums


def evaluate(model, data_loader, device):
  data_loader = tqdm(data_loader)
  scores = []
  labels = []

  for step, batch in enumerate(data_loader):
    model.eval()
    batch = tuple(t.to(device) for t in batch)
    input_ids, attention_mask, ans_pos, choice_label, label = batch
    score = model(input_ids, attention_mask, ans_pos, choice_label, label, "val")
    scores += score.tolist()
    labels += label.tolist()

  scores = torch.tensor(scores)

  top1_indices = torch.topk(scores, k=1).indices.tolist()
  hit1 = get_hit_scores(top1_indices, labels)
  top5_indices = torch.topk(scores, k=5).indices.tolist()
  hit5 = get_hit_scores(top5_indices, labels)

  return hit1, hit5


def train(samples_train, samples_dev, samples_test, args):
  set_seeds(args)
  best_val_perf = float('-inf')
  logger = Logger(args.model + '.log', on=True)
  logger.log(str(args))
  device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  args.device = device
  logger.log(f'Using device: {str(device)}', force=True)
  entities = load_entities(args.dataset + args.kb_path)
  logger.log('number of entities {:d}'.format(len(entities)))

  tokenizer = RobertaTokenizer.from_pretrained(args.pretrained_model)
  special_tokens = ["<txcla>", '[or]', "[NIL]"]
  sel_tokens = [f"[{i}]" for i in range(args.cand_num)]
  special_tokens += sel_tokens
  tokenizer.add_special_tokens({'additional_special_tokens': special_tokens})
  args.tokenizer = tokenizer
  model = load_model(True, device, args.type_loss, args)

  num_train_samples = len(samples_train)
  if args.simpleoptim:
    optimizer, scheduler, num_train_steps, num_warmup_steps \
      = configure_optimizer_simple(args, model, num_train_samples)
  else:
    optimizer, scheduler, num_train_steps, num_warmup_steps \
      = configure_optimizer(args, model, num_train_samples)
  args.n_gpu = torch.cuda.device_count()
  model.to(device)
  dp = args.n_gpu > 1
  if dp:
    logger.log('Data parallel across {:d} GPUs {:s}'
               ''.format(len(args.gpus.split(',')), args.gpus))
    model = nn.DataParallel(model)

  train_loader = get_prompt_mention_loader(samples_train, entities, tokenizer, True, False, args)
  dev_loader = get_prompt_mention_loader(samples_dev, entities, tokenizer, False, True, args)
  test_loader = get_prompt_mention_loader(samples_test, entities, tokenizer, False, True, args)
  model.train()
  effective_bsz = args.batch * args.gradient_accumulation_steps
  # train
  logger.log('***** train *****')
  logger.log('# train samples: {:d}'.format(num_train_samples))
  logger.log('# val samples: {:d}'.format(len(samples_dev)))

  logger.log('# test samples: {:d}'.format(len(samples_test)))
  logger.log('# epochs: {:d}'.format(args.epochs))
  logger.log(' batch size : {:d}'.format(args.batch))
  logger.log(' gradient accumulation steps {:d}'
             ''.format(args.gradient_accumulation_steps))
  logger.log(
    ' effective training batch size with accumulation: {:d}'
    ''.format(effective_bsz))
  logger.log(' # training steps: {:d}'.format(num_train_steps))
  logger.log(' # warmup steps: {:d}'.format(num_warmup_steps))
  logger.log(' learning rate: {:g}'.format(args.lr))
  logger.log(' # parameters: {:d}'.format(count_parameters(model)))

  step_num = 0
  tr_loss, logging_loss = 0.0, 0.0
  start_epoch = 1
  #
  model.zero_grad()

  for epoch in range(start_epoch, args.epochs + 1):
    logger.log('\nEpoch {:d}'.format(epoch))

    epoch_start_time = datetime.now()

    epoch_train_start_time = datetime.now()
    train_loader = tqdm(train_loader)
    for step, batch in enumerate(train_loader):
      model.train()
      bsz = batch[0].size(0)

      batch = tuple(t.to(device) for t in batch)
      text_token_ids, attention_masks, ans_pos, choice_label, labels = batch
      loss = model(text_token_ids, attention_masks, ans_pos, choice_label, labels, "train")
      if dp:
        loss = loss.sum() / bsz
      else:
        loss /= bsz

      loss_avg = loss / args.gradient_accumulation_steps

      loss_avg.backward()
      tr_loss += loss_avg.item()

      if (step + 1) % args.gradient_accumulation_steps == 0:
        torch.nn.utils.clip_grad_norm_(model.parameters(),
                                       args.clip)
        optimizer.step()
        scheduler.step()
        model.zero_grad()
        step_num += 1

    logger.log('training time for epoch {:3d} '
               'is {:s}'.format(epoch, strtime(epoch_train_start_time)))

    hit1, hit5 = evaluate(model, dev_loader, device)
    logger.log('Done with epoch {:3d} | train loss {:8.4f} | '
               'recall@1 {:8.4f}|'
               'recall@5 {:8.4f}'
               ' epoch time {} '.format(
      epoch,
      tr_loss / step_num,
      hit1,
      hit5,
      strtime(epoch_start_time)
    ))
    save_model = (hit1 >= best_val_perf)
    if save_model:
      current_best = hit1
      logger.log('------- new best val perf: {:g} --> {:g} '
                 ''.format(best_val_perf, current_best))
      best_val_perf = current_best

      torch.save({'opt': args,
                  'sd': model.module.state_dict() if dp else model.state_dict(),
                  # 'sd': model.state_dict(),
                  'perf': best_val_perf, 'epoch': epoch,
                  'opt_sd': optimizer.state_dict(),
                  'scheduler_sd': scheduler.state_dict(),
                  'tr_loss': tr_loss, 'step_num': step_num,
                  'logging_loss': logging_loss},
                 args.model)
    else:
      logger.log('')
  model = load_model(False, device, args.type_loss, args)

  model.to(device)
  save_prompt_predict_test(model, samples_test, entities, tokenizer, device, args)
  hit1, hit5 = evaluate(model, test_loader, device)
  logger.log(f"test acc @1: {hit1},   test acc @5: {hit5}")


def shuffle_data(data):
  for i in range(len(data)):
    d = data[i]
    labels = d["mention_data"]["labels"]
    candidates = d["mention_data"]["candidates"]
    can_las = [(candidate, label) for candidate, label in zip(candidates, labels)]
    random.shuffle(can_las)
    candidates = [can_la[0] for can_la in can_las]
    labels = [can_la[1] for can_la in can_las]
    d["mention_data"]["labels"] = labels
    d["mention_data"]["candidates"] = candidates


def main(args):
  train_data = load_data(args.dataset + args.train_data)
  dev_data = load_data(args.dataset + args.dev_data)
  test_data = load_data(args.dataset + args.test_data)
  train(train_data, dev_data, test_data, args)


if __name__ == '__main__':
  parser = argparse.ArgumentParser()

  parser.add_argument("--dataset",
                      default="dataset/ncbi-disease/")
  parser.add_argument("--model",
                      default="model_disambiguation/ncbi_disambiguation_prompt_pretrain.pt")
  parser.add_argument("--pretrained_model",
                      default="iHealthGroup/shc-cn-roberta-lm")
  parser.add_argument("--use_pretrained_model", action="store_true")
  parser.add_argument("--pretrained_model_path",
                      default="model_pretrain/bc5cdr_pretrain.pt")

  parser.add_argument("--type_loss", type=str,
                      default="sum_log_nce",
                      choices=["log_sum", "sum_log", "sum_log_nce",
                               "max_min", "bce_loss"])
  parser.add_argument("--max_len", default=512)
  parser.add_argument("--max_ent_len", default=32)
  parser.add_argument("--max_text_len", default=256)

  parser.add_argument("--train_data", default="disambiguation_output/train.json")
  parser.add_argument("--dev_data", default="disambiguation_output/dev.json")
  parser.add_argument("--test_data", default="disambiguation_output/test.json")
  parser.add_argument("--kb_path", default="entity_kb.json")

  parser.add_argument("--batch", default=1, type=int)
  parser.add_argument("--lr", default=5e-5, type=float)
  parser.add_argument("--epochs", default=1, type=int)
  parser.add_argument("--cand_num", default=6)
  parser.add_argument("--warmup_proportion", default=0.2)
  parser.add_argument("--weight_decay", default=0.01)
  parser.add_argument("--adam_epsilon", default=1e-6, type=float)
  parser.add_argument("--gradient_accumulation_steps", default=2)
  parser.add_argument("--seed", default=42, type=int)
  parser.add_argument("--num_workers", default=0)
  parser.add_argument("--simpleoptim", default=False)
  parser.add_argument("--clip", default=1)

  parser.add_argument("--gpus", default="0")
  parser.add_argument("--logging_steps", default=100)
  parser.add_argument("--temperature", default=16, type=int)
  args = parser.parse_args()
  # Set environment variables before all else.
  # Sets torch.cuda behavior
  os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus
  main(args)
