import torch
import torch.nn.functional as F
import numpy as np


def _rescale(tensor, size):
    return F.interpolate(
        input=tensor.unsqueeze(0).float(), 
        size=size, 
        mode='bilinear',
        align_corners=False).squeeze(0).to(tensor.dtype)
        

def _pad(tensor, size, value=0.):
    xpad = size[1] - tensor.shape[2]
    ypad = size[0] - tensor.shape[1]

    # not that F.pad takes sizes starting from the last dimension
    padded = F.pad(
        tensor.float(),
        (0, xpad, 0, ypad),
        mode='constant',
        value=value
    ).to(tensor.dtype)

    assert padded.shape[1:] == tuple(size)
    return padded

class Image:
    def __init__(self, K, R, T, bitmap, depth, mask=None):
        self.K = K
        self.R = R
        self.T = T
        self.depth = depth
        self.bitmap = bitmap

        if mask is None:
            self.mask = torch.ones(bitmap.shape[-2:]).bool()
        else:
            self.mask = mask.bool()
    
    @property
    def K_inv(self):
        return self.K.inverse()
    
    @property
    def hwc(self):
        # origin bitmap shape: [c, h, w]
        return self.bitmap.permute(1, 2, 0)
    
    @property
    def shape(self):
        return self.bitmap.shape[-2:]

    @property
    def orishape(self):
        return torch.tensor(self.bitmap.shape[-2:])
    
    @property
    def length(self):
        return self.bitmap.shape[-2] * self.bitmap.shape[-1]
    

    def to(self, *args, **kwargs):
        TRANSFERRED_ATTRS = ['K', 'R', 'T', 'depth', 'mask']

        for key in TRANSFERRED_ATTRS:
            attr = getattr(self, key)
            if attr is not None:
                attr_transferred = attr.to(*args, **kwargs)
            setattr(self, key, attr_transferred)

        return self
    

    def scale(self, size):
        x_factor = self.shape[0] / size[0]
        y_factor = self.shape[1] / size[1]

        f = 1 / max(x_factor, y_factor)
        if x_factor > y_factor:
            new_size = (size[0], int(f * self.shape[1]))
        else:
            new_size = (int(f * self.shape[0]), size[1])
        
        K_scaler = torch.tensor([
            [f, 0, f],
            [0, f, f],
            [0, 0, 1]
        ], dtype=self.K.dtype, device=self.K.device)
        K = K_scaler @ self.K

        bitmap = _rescale(self.bitmap, new_size)
        depth = _rescale(self.depth, new_size)
        mask = _rescale(self.mask[None], new_size)[0]

        return Image(K, self.R, self.T, bitmap, depth, mask)
    

    def pad(self, size):
        bitmap = _pad(self.bitmap, size, value=0)
        depth = _pad(self.depth, size, value=float('NaN'))
        mask = _pad(self.mask[None], size, value=0)[0]

        return Image(self.K, self.R, self.T,  bitmap, depth, mask)
    

    def in_range_mask(self, xy):
        h, w = self.shape
        x, y = xy  # xy [2, num]: x所有num个x的坐标， y是所有num个y的坐标

        return (x >= 0) & (y >= 0) & (x <= w) & (y <= h)
    

    def fetch_depth(self, xy):
        in_range = self.in_range_mask(xy)
        finite = torch.isfinite(xy).all(dim=0)
        valid_depth = in_range & finite
        x, y  = xy[:, valid_depth].to(torch.int64)
        # depth是形状为[nums], 值为Nan
        depth = torch.full(
            (xy.shape[1], ),
            fill_value=float('NaN'),
            device=xy.device,
            dtype=self.depth.dtype
        )
        # 用valid作为mask找到需要得到深度值的地方，然后填入depth的真实值,即拿到所有特征点对应的深度值
        depth[valid_depth] = self.depth[0, y, x]
        return depth
    
    def unproject(self, xy):
        depth = self.fetch_depth(xy)

        xyw = torch.cat([
            xy.to(depth.dtype),
            torch.ones(1, xy.shape[1], dtype=depth.dtype, device=xy.device)
        ], dim=0)

        xyz = (self.K_inv @ xyw) * depth
        xyz_w = self.R.T @ (xyz - self.T[:, None])

        return xyz_w


    def project(self, xyw):
        # [3,N] -> [2,N]
        extrinsic = self.R @ xyw + self.T[:, None]
        intrinsic = self.K @ extrinsic
        return intrinsic[:2] / intrinsic[2]