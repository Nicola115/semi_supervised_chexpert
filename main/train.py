import _init_paths
import sys
from loss import *
from dataset import *
from config import cfg, update_config
from utils.utils import (
    create_logger,
    get_optimizer,
    get_scheduler,
    get_model,
    get_category_list,
)
from core.function import train_model, valid_model
from core.combiner import Combiner

import torch
import os, shutil
from torch.utils.data import DataLoader
import argparse
import warnings
import click
from tensorboardX import SummaryWriter
import torch.backends.cudnn as cudnn
import ast
from datetime import datetime

from apex.parallel import DistributedDataParallel as DDP
from apex.fp16_utils import *
from apex import amp, optimizers
from apex.multi_tensor_apply import multi_tensor_applier
import random
import numpy as np
import json
import ipdb


def parse_args():
    parser = argparse.ArgumentParser(description="codes for BBN")

    parser.add_argument(
        "--cfg",
        help="decide which cfg to use",
        required=False,
        default="configs/cifar10.yaml",
        type=str,
    )
    parser.add_argument(
        "--ar",
        help="decide whether to use auto resume",
        type= ast.literal_eval,
        dest = 'auto_resume',
        required=False,
        default= True,
    )

    parser.add_argument(
        "--local_rank",
        help='local_rank for distributed training',
        type=int,
        default=0,
    )

    parser.add_argument(
        "opts",
        help="modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    local_rank = args.local_rank
    rank = local_rank
    update_config(cfg, args)
    logger, log_file = create_logger(cfg, local_rank)
    warnings.filterwarnings("ignore")
    auto_resume = args.auto_resume

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)

    cudnn.deterministic = True
    cudnn.benchmark = False

    # close loop
    model_dir = os.path.join(cfg.OUTPUT_DIR, cfg.NAME, "models",
                             str(datetime.now().strftime("%Y-%m-%d-%H-%M")))
    code_dir = os.path.join(cfg.OUTPUT_DIR, cfg.NAME, "codes",
                             str(datetime.now().strftime("%Y-%m-%d-%H-%M")))
    tensorboard_dir = (
        os.path.join(cfg.OUTPUT_DIR, cfg.NAME, "tensorboard",
                             str(datetime.now().strftime("%Y-%m-%d-%H-%M")))
        if cfg.TRAIN.TENSORBOARD.ENABLE
        else None
    )
    if local_rank == 0:

        if not os.path.exists(model_dir):
            os.makedirs(model_dir)
        else:
            logger.info(
                "This directory has already existed, Please remember to modify your cfg.NAME"
            )
            if not click.confirm(
                "\033[1;31;40mContinue and override the former directory?\033[0m",
                default=False,
            ):
                exit(0)
            shutil.rmtree(code_dir)
            if tensorboard_dir is not None and os.path.exists(tensorboard_dir):
                shutil.rmtree(tensorboard_dir)
        print("=> output model will be saved in {}".format(model_dir))
        this_dir = os.path.dirname(__file__)
        ignore = shutil.ignore_patterns(
            "*.pyc", "*.so", "*.out", "*pycache*", "*.pth", "*build*", "*output*", "*datasets*"
        )
        shutil.copytree(os.path.join(this_dir, ".."), code_dir, ignore=ignore)

    if cfg.TRAIN.DISTRIBUTED:
        if local_rank == 0:
            print('Init the process group for distributed training')
        torch.cuda.set_device(local_rank)
        torch.distributed.init_process_group(backend='nccl',
                                             init_method='env://')
        if local_rank == 0:
            print('Init complete')


    train_set = eval(cfg.DATASET.DATASET)("train", cfg)
    valid_set = eval(cfg.DATASET.DATASET)("valid", cfg)

    annotations = train_set.get_annotations()
    num_classes = train_set.get_num_classes()
    device = torch.device("cpu" if cfg.CPU_MODE else "cuda")

    num_class_list, cat_list = get_category_list(annotations, num_classes, cfg)
    if local_rank == 0:
        with open('train_data_statistic.json','w') as outfile:
            json.dump(num_class_list,outfile)

    para_dict = {
        "num_classes": num_classes,
        "num_class_list": num_class_list,
        "cfg": cfg,
        "device": device,
    }

    criterion = eval(cfg.LOSS.LOSS_TYPE)(para_dict=para_dict)
    epoch_number = cfg.TRAIN.MAX_EPOCH

    # ----- BEGIN MODEL BUILDER -----
    model = get_model(cfg, num_classes, device, logger)
    optimizer = get_optimizer(cfg, model)
    scheduler = get_scheduler(cfg, optimizer)
    if cfg.TRAIN.DISTRIBUTED:
        model = model.cuda()
        if 'res50_sw' not in cfg.BACKBONE.TYPE:
            model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
        model = DDP(model, delay_allreduce=True)

    if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
        model2 = get_model(cfg, num_classes, device, logger)
        optimizer2 = get_optimizer(cfg, model2)
        scheduler2 = get_scheduler(cfg, optimizer2)
        if cfg.TRAIN.DISTRIBUTED:
            model2 = model2.cuda()
            if 'res50_sw' not in cfg.BACKBONE.TYPE:
                model2, optimizer2 = amp.initialize(model2, optimizer2, opt_level='O1')
            model2 = DDP(model2, delay_allreduce=True)
    combiner = Combiner(cfg, device)

    
    # ----- END MODEL BUILDER -----

    if cfg.TRAIN.DISTRIBUTED:
        train_sampler = torch.utils.data.DistributedSampler(train_set)
        val_sampler = torch.utils.data.DistributedSampler(valid_set)
        trainLoader = DataLoader(
            train_set,
            batch_size=cfg.TRAIN.BATCH_SIZE,
            shuffle=False,
            num_workers=cfg.TRAIN.NUM_WORKERS,
            pin_memory=False,
            sampler=train_sampler,
        )
        validLoader = DataLoader(
            valid_set,
            batch_size=cfg.TEST.BATCH_SIZE,
            shuffle=False,
            num_workers=cfg.TEST.NUM_WORKERS,
            pin_memory=False,
            sampler=val_sampler,
        )

    else:
        trainLoader = DataLoader(
            train_set,
            batch_size=cfg.TRAIN.BATCH_SIZE,
            shuffle=cfg.TRAIN.SHUFFLE,
            num_workers=cfg.TRAIN.NUM_WORKERS,
            pin_memory=cfg.PIN_MEMORY,
            drop_last=True
        )

        validLoader = DataLoader(
            valid_set,
            batch_size=cfg.TEST.BATCH_SIZE,
            shuffle=False,
            num_workers=cfg.TEST.NUM_WORKERS,
            pin_memory=cfg.PIN_MEMORY,
        )


    if tensorboard_dir is not None and local_rank == 0:
        dummy_input = torch.rand((1, 3) + cfg.INPUT_SIZE).to(device)
        writer = SummaryWriter(log_dir=tensorboard_dir)
        writer.add_graph(model if cfg.CPU_MODE else model.module, (dummy_input,))
        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            writer.add_graph(model2 if cfg.CPU_MODE else model2.module,(dummy_input,))
    else:
        writer = None

    best_result, best_epoch, start_epoch = 0, 0, 1

    # ----- BEGIN RESUME ---------
    all_models = os.listdir(model_dir)
    # print(all_models)
    # print(auto_resume)
    if len(all_models) <= 1 or auto_resume == False:
        auto_resume = False
    else:
        all_models.remove("best_model.pth")
        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            all_models.remove("best_model2.pth")

        resume_epoch = max([int(name.split(".")[0].split("_")[-1]) for name in all_models])
        resume_model_path = os.path.join(model_dir, "epoch_{}.pth".format(resume_epoch))
        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            resume_model_path2 = os.path.join(model_dir, "2_epoch_{}.pth".format(resume_epoch))

    if cfg.RESUME_MODEL != "" or auto_resume:
        if cfg.RESUME_MODEL == "":
            resume_model = resume_model_path
        else:
            resume_model = cfg.RESUME_MODEL if '/' in cfg.RESUME_MODEL else os.path.join(model_dir, cfg.RESUME_MODEL)
        logger.info("Loading checkpoint from {}...".format(resume_model))
        checkpoint = torch.load(
            resume_model, map_location="cpu" if cfg.CPU_MODE or cfg.TRAIN.DISTRIBUTED else "cuda"
        )
        if cfg.CPU_MODE:
            model.load_model(resume_model)
        else:
            model.module.load_model(resume_model)
        if cfg.RESUME_MODE != "state_dict":
            optimizer.load_state_dict(checkpoint['optimizer'])
            scheduler.load_state_dict(checkpoint['scheduler'])
            start_epoch = checkpoint['epoch'] + 1
            best_result = checkpoint['best_result']
            best_epoch = checkpoint['best_epoch']

        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            if cfg.RESUME_MODEL2 == "":
                resume_model2 = resume_model_path2
            else:
                resume_model2 = cfg.RESUME_MODEL2 if '/' in cfg.RESUME_MODEL2 else os.path.join(model_dir, cfg.RESUME_MODEL2)
            logger.info("Loading checkpoint from {}...".format(resume_model2))
            checkpoint2 = torch.load(
                resume_model2, map_location="cpu" if cfg.CPU_MODE or cfg.TRAIN.DISTRIBUTED else "cuda"
            )
            if cfg.CPU_MODE:
                model2.load_model(resume_model2)
            else:
                model2.module.load_model(resume_model2)
            if cfg.RESUME_MODE != "state_dict":
                optimizer2.load_state_dict(checkpoint2['optimizer'])
                scheduler2.load_state_dict(checkpoint2['scheduler'])
                assert checkpoint2['epoch'] + 1 == start_epoch
                assert checkpoint2['best_result'] == best_result
                assert checkpoint2['best_epoch'] == best_epoch
        
    # ----- END RESUME ---------

    if rank == 0:
        logger.info(
            "-------------------Train start :{}  {}  {}-------------------".format(
                cfg.BACKBONE.TYPE, cfg.MODULE.TYPE, cfg.TRAIN.COMBINER.TYPE
            )
        )

    for epoch in range(start_epoch, epoch_number + 1):
        if cfg.TRAIN.DISTRIBUTED:
            train_sampler.set_epoch(epoch)
        scheduler.step()
        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            scheduler2.step()
            train_acc, train_loss, train_acc2, train_loss2 = train_model(
                trainLoader,
                (model,model2),
                epoch,
                epoch_number,
                (optimizer,optimizer2),
                combiner,
                criterion,
                cfg,
                logger,
                writer=writer,
                rank=local_rank,
            )
        else:
            train_acc, train_loss = train_model(
                trainLoader,
                model,
                epoch,
                epoch_number,
                optimizer,
                combiner,
                criterion,
                cfg,
                logger,
                writer=writer,
                rank=local_rank,
            )

        
        model_save_path = os.path.join(
            model_dir,
            "epoch_{}.pth".format(epoch),
        )
        if epoch % cfg.SAVE_STEP == 0 and local_rank == 0:
            torch.save({
                'state_dict': model.state_dict(),
                'epoch': epoch,
                'best_result': best_result,
                'best_epoch': best_epoch,
                'scheduler': scheduler.state_dict(),
                'optimizer': optimizer.state_dict()
            }, model_save_path)

        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            model2_save_path = os.path.join(
                model_dir,
                "2_epoch_{}.pth".format(epoch),
            )
            if epoch % cfg.SAVE_STEP == 0 and local_rank == 0:
                torch.save({
                    'state_dict': model2.state_dict(),
                    'epoch': epoch,
                    'best_result': best_result,
                    'best_epoch': best_epoch,
                    'scheduler': scheduler2.state_dict(),
                    'optimizer': optimizer2.state_dict()
                }, model2_save_path)

        loss_dict, acc_dict = {"train_loss": train_loss}, {"train_acc": train_acc}
        if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
            loss_dict2, acc_dict2 = {"train_loss": train_loss2}, {"train_acc": train_acc2}

        if cfg.VALID_STEP != -1 and epoch % cfg.VALID_STEP == 0:
            if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
                valid_acc, valid_loss, valid_acc2, valid_loss2 = valid_model(
                    validLoader, epoch, (model,model2), cfg, criterion, logger, device, rank=rank, writer=writer
                )
            else:
                valid_acc, valid_loss = valid_model(
                    validLoader, epoch, model, cfg, criterion, logger, device, rank=rank, writer=writer
                )
            loss_dict["valid_loss"], acc_dict["valid_acc"] = valid_loss, valid_acc
            if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
                loss_dict2["valid_loss"], acc_dict2["valid_acc"] = valid_loss2, valid_acc2

            # TODO:save best result only according to model1 result
            if valid_acc > best_result and local_rank == 0:
                best_result, best_epoch = valid_acc, epoch
                torch.save({
                        'state_dict': model.state_dict(),
                        'epoch': epoch,
                        'best_result': best_result,
                        'best_epoch': best_epoch,
                        'scheduler': scheduler.state_dict(),
                        'optimizer': optimizer.state_dict(),
                }, os.path.join(model_dir, "best_model.pth")
                )
                if cfg.TRAIN.COMBINER.TYPE == 'coteaching':
                    torch.save({
                        'state_dict': model2.state_dict(),
                        'epoch': epoch,
                        'best_result': best_result,
                        'best_epoch': best_epoch,
                        'scheduler': scheduler2.state_dict(),
                        'optimizer': optimizer2.state_dict(),
                    }, os.path.join(model_dir, "best_model2.pth")
                    )

            if rank == 0:
                logger.info(
                    "--------------Best_Epoch:{:>3d}    Best_Acc:{:>5.2f}%--------------".format(
                        best_epoch, best_result * 100
                    )
                )
        if cfg.TRAIN.TENSORBOARD.ENABLE and local_rank == 0:
            writer.add_scalars("scalar/acc", acc_dict, epoch)
            writer.add_scalars("scalar/loss", loss_dict, epoch)
            writer.add_scalars("scalar/acc2", acc_dict2, epoch)
            writer.add_scalars("scalar/loss2", loss_dict2, epoch)
    if cfg.TRAIN.TENSORBOARD.ENABLE and local_rank == 0:
        writer.close()
    if rank == 0:
        logger.info(
            "-------------------Train Finished :{}-------------------".format(cfg.NAME)
        )
