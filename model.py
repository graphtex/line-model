from tensorboardX import SummaryWriter
import torch
import sys
from torch.utils.data import DataLoader, random_split
from dataset import GraphImageDataSet, GraphTestImages
from letr_model.models.letr import SetCriterion
from letr_model.models.matcher import HungarianMatcher_Line as HungarianMatcher
from letr_model.util.misc import collate_fn
import utils
import tomli
from torchvision.utils import make_grid
import torchvision.transforms as T

from letr_model.models import build_model


def build(args, model_file=None):
    device = args['training']['device']

    checkpoint = torch.load('./exp/res50_stage2_focal/checkpoints/checkpoint0024.pth', map_location='cpu')

    # load model
    args_model = checkpoint['args']
    args_model.device=device
    model, _, postprocessors = build_model(args_model)
    model.load_state_dict(checkpoint['model'])
    model.eval()


    # model = torch.hub.load('facebookresearch/detr:main', 'detr_resnet50', pretrained=True)
    # if model_file is not None:
    #   print("LOADED MODEL")
    #   state_dict = torch.load(model_file, map_location=torch.device(device))
    #   model.load_state_dict(state_dict)
    
    model.to(device)

    #TODO: replace 1 with 0?
    matcher = HungarianMatcher(args['loss']['matcher']['set_cost_class'],args['loss']['matcher']['set_cost_line'])
    weight_dict = {'loss_ce': 1, 'loss_line': 1} #TODO: could adjust
    losses = ['POST_lines_labels','POST_lines', 'cardinality']
    criterion = SetCriterion(1, weight_dict, args['loss']['coef']['eos'], losses, args_model, matcher)
    criterion.to(device)

    return model, criterion, postprocessors


def train_one_epoch(model, criterion, postprocessors, data_loader, optim, device, writer, step):

    model.train()
    criterion.train()

    for samples, targets in data_loader:
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        outputs, origin_indices = model(samples, postprocessors, targets, criterion)
        loss_dict = criterion(outputs, targets, origin_indices)
        weight_dict = criterion.weight_dict
        loss = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        optim.zero_grad()
        loss.backward()
        #TODO: original DETR includes gradient clipping here
        optim.step()

        for k,v in loss_dict.items():
            writer.add_scalar(f'train/loss/{k}', v, step)
        writer.add_scalar("train/loss/total", loss, step)
        if step % 10 == 0:
            print(f"step {step} - loss {loss.item()}")
        step += 1
    
    return step

@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, device, writer, epoch):
    loss_ce = 0
    loss_bbox = 0
    loss_tot = 0
    loss_giou = 0

    step = 0
    l = len(data_loader)
    for samples, targets in data_loader:
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        outputs, origin_indices = model(samples, postprocessors, targets, criterion)
        loss_dict = criterion(outputs, targets, origin_indices)
        weight_dict = criterion.weight_dict
        
        loss = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # loss_ce += loss_dict['loss_ce'].item()
        # loss_bbox += loss_dict['loss_bbox'].item()
        # loss_giou += loss_dict['loss_giou'].item()
        loss_tot += loss.item()
        
        if step % 10 == 0:
            print(f"test step {step} - loss {loss.item()}")
        step += 1

    # writer.add_scalar(f'test/loss/ce', loss_ce / l, epoch) 
    # writer.add_scalar(f'test/loss/bbox', loss_ce / l, epoch) 
    # writer.add_scalar(f'test/loss/giou', loss_ce / l, epoch) 
    writer.add_scalar('test/loss/total', loss / l, epoch)


    
@torch.no_grad()
def gen_test_segmentations(model, data_loader, device, thresh=0.7):
    imgs = []
    for image in data_loader:
        image = image.to(device)
        outputs = model(image)
        ps = outputs['pred_logits'].softmax(-1)[0, :, :-1]
        print(f"Max p: {ps.max()}")
        keep = ps.max(-1).values > thresh

        boxes = outputs['pred_boxes'][0,keep]
        boxes = box_convert(boxes, 'cxcywh', 'xyxy')
        img_bytes = T.functional.convert_image_dtype(image[0], torch.uint8)
        _,Y,X = img_bytes.shape
        scale = torch.Tensor([X,Y,X,Y]).to(device)
        boxes = torch.einsum('ij,j->ij',boxes,scale)
        boxes = boxes.to(torch.int)

        b = draw_bounding_boxes(img_bytes, boxes, width=3)
        b = b.to('cpu')
        imgs.append(b)

    grid = make_grid(imgs, nrow=2)
    return grid


def run(args, model_file=None):
    dataset = GraphImageDataSet(args['dataset']['annot_file'], args['dataset']['img_dir'])
    test_dataset = GraphTestImages(args['dataset']['test_img_dir'])
    train, val = random_split(dataset, [0.8,0.2], generator=torch.Generator().manual_seed(42))
    print(f"train len: {len(train)}")
    print(f"val len: {len(val)}")

    model, criterion, postprocessors = build(args, model_file)

    param_dicts = [
        {"params": [p for n, p in model.named_parameters() if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args['optim']['lr_backbone'],
        },
    ]
    optim = torch.optim.AdamW(param_dicts, lr=args['optim']['lr'],
                                  weight_decay=args['optim']['weight_decay'])
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optim, args['optim']['lr_drop'])

    data_loader_train = DataLoader(train, args['training']['batch_size'], num_workers=2, collate_fn=collate_fn)
    data_loader_val = DataLoader(val, args['training']['batch_size'], num_workers=2, collate_fn=collate_fn)
    data_loader_test = DataLoader(test_dataset, batch_size=1, num_workers=2)

    writer = SummaryWriter(args['training']['logdir'])

    device = args['training']['device']
    step = 0
    for epoch in range(args['training']['start_epoch'], args['training']['epochs']+1):
        step = train_one_epoch(model, criterion, postprocessors, data_loader_train, optim, device, writer, step)
        lr_scheduler.step()
    
        evaluate(model, criterion, postprocessors, data_loader_val, device, writer, epoch)
        model_filename = f"{args['training']['logdir']}/model_epoch_{epoch}.pt"
        torch.save(model.state_dict(), model_filename)
        # img = gen_test_segmentations(model, data_loader_test, device)
        # writer.add_image("test/segmentation", img, epoch)
        
    
    return model


if __name__ == "__main__":
    with open(sys.argv[1], "rb") as f:
        args = tomli.load(f)
        print(args)
    run(args)




