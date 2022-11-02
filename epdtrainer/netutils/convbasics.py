import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.quantization import QuantStub, DeQuantStub
from torchvision.ops.misc import ConvNormActivation


def ConvBlock(
        in_channels,
        out_channels,
        kernel_size=3,
        stride=1,
        pad=1,
        norm=nn.BatchNorm2d,
        act=nn.ReLU6):
    layers = [
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size,
            stride=stride,
            padding=pad)
        # nn.BatchNorm2d(out_channels),
    ]
    if(norm):
        layers.append(
            norm(out_channels)
        )
    if(act):
        layers.append(act())
    return nn.Sequential(*layers)


class ConvBNReLU(nn.Sequential):
    def __init__(self, in_planes, out_planes, kernel_size=3, stride=1, pad=1, groups=1, bias=False, dilation=1):
        super(ConvBNReLU, self).__init__(
            nn.Conv2d(in_planes, out_planes, kernel_size, stride,
                      pad, groups=groups, bias=bias, dilation=dilation),
            nn.BatchNorm2d(out_planes, momentum=0.1),
            # Replace with ReLU
            nn.ReLU6(inplace=True)
        )


class BNReLU(nn.Sequential):
    def __init__(self, out_planes):
        super(ConvBNReLU, self).__init__(
            nn.BatchNorm2d(out_planes, momentum=0.1),
            # Replace with ReLU
            nn.ReLU6(inplace=True)
        )


class ConvBN(nn.Sequential):
    def __init__(self, in_planes, out_planes, kernel_size=3, stride=1, pad=1, groups=1, bias=False, dilation=1):
        super(ConvBN, self).__init__(
            nn.Conv2d(in_planes, out_planes, kernel_size, stride,
                      pad, groups=groups, bias=bias, dilation=dilation),
            nn.BatchNorm2d(out_planes, momentum=0.1),
        )


class UpSample(nn.Module):

    def __init__(self, scale_factor=2, mode="bilinear"):
        super().__init__()
        self.scale_factor = scale_factor
        self.mode = mode

    def forward(self, x):
        return F.interpolate(x, scale_factor=self.scale_factor, mode=self.mode)


class SiLU(nn.Module):
    """export-friendly version of nn.SiLU()"""

    @staticmethod
    def forward(x):
        return x * torch.sigmoid(x)


def get_activation(name="silu", inplace=True):
    if name == "silu":
        module = nn.SiLU(inplace=inplace)
    elif name == "relu":
        module = nn.ReLU(inplace=inplace)
    elif name == "lrelu":
        module = nn.LeakyReLU(0.1, inplace=inplace)
    else:
        raise AttributeError("Unsupported act type: {}".format(name))
    return module


class BaseConv(nn.Module):
    """A Conv2d -> Batchnorm -> silu/leaky relu block"""

    def __init__(
        self, in_channels, out_channels, ksize, stride, groups=1, bias=False, act="silu"
    ):
        super().__init__()
        # same padding
        pad = (ksize - 1) // 2
        self.conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=ksize,
            stride=stride,
            padding=pad,
            groups=groups,
            bias=bias,
        )
        self.bn = nn.BatchNorm2d(out_channels)
        self.act = get_activation(act, inplace=True)

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

    def fuseforward(self, x):
        return self.act(self.conv(x))


class DWConv(nn.Module):
    """Depthwise Conv + Conv"""

    def __init__(self, in_channels, out_channels, ksize, stride=1, act="silu"):
        super().__init__()
        self.dconv = BaseConv(
            in_channels,
            in_channels,
            ksize=ksize,
            stride=stride,
            groups=in_channels,
            act=act,
        )
        self.pconv = BaseConv(
            in_channels, out_channels, ksize=1, stride=1, groups=1, act=act
        )

    def forward(self, x):
        x = self.dconv(x)
        return self.pconv(x)


class Bottleneck(nn.Module):
    # Standard bottleneck
    def __init__(
        self,
        in_channels,
        out_channels,
        shortcut=True,
        expansion=0.5,
        depthwise=False,
        act="silu",
        qat=False
    ):
        super().__init__()
        hidden_channels = int(out_channels * expansion)
        Conv = DWConv if depthwise else BaseConv
        self.conv1 = BaseConv(
            in_channels, hidden_channels, 1, stride=1, act=act)
        self.conv2 = Conv(hidden_channels, out_channels, 3, stride=1, act=act)
        self.use_add = shortcut and in_channels == out_channels
        self.qat = qat
        self.qat_func = nn.quantized.FloatFunctional()

    def forward(self, x):
        y = self.conv2(self.conv1(x))
        if self.use_add:
            if self.qat:
                y = self.qat_func.add(y, x)
            else:
                y = y + x
        return y


class ResLayer(nn.Module):
    "Residual layer with `in_channels` inputs."

    def __init__(self, in_channels: int):
        super().__init__()
        mid_channels = in_channels // 2
        self.layer1 = BaseConv(
            in_channels, mid_channels, ksize=1, stride=1, act="lrelu"
        )
        self.layer2 = BaseConv(
            mid_channels, in_channels, ksize=3, stride=1, act="lrelu"
        )

    def forward(self, x):
        out = self.layer2(self.layer1(x))
        return x + out


class SPPBottleneck(nn.Module):
    """Spatial pyramid pooling layer used in YOLOv3-SPP"""

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_sizes=(5, 9, 13),
        activation="silu",
        qat=False
    ):
        super().__init__()
        self.qat = qat
        self.qat_func = nn.quantized.FloatFunctional()
        hidden_channels = in_channels // 2
        self.conv1 = BaseConv(in_channels, hidden_channels,
                              1, stride=1, act=activation)
        self.m = nn.ModuleList(
            [
                nn.MaxPool2d(kernel_size=ks, stride=1, padding=ks // 2)
                for ks in kernel_sizes
            ]
        )
        conv2_channels = hidden_channels * (len(kernel_sizes) + 1)
        self.conv2 = BaseConv(conv2_channels, out_channels,
                              1, stride=1, act=activation)

    def forward(self, x):
        x = self.conv1(x)
        if self.qat:
            x = self.qat_func.cat([x] + [m(x) for m in self.m], dim=1)
        else:
            x = torch.cat([x] + [m(x) for m in self.m], dim=1)
        x = self.conv2(x)
        return x


class SPPFBottleneck(nn.Module):
    """Spatial pyramid pooling fast layer"""

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_sizes=(5, 5, 5),
        activation="silu",
        qat=False
    ):
        super().__init__()
        self.qat = qat
        self.qat_func = nn.quantized.FloatFunctional()
        hidden_channels = in_channels // 2
        self.conv1 = BaseConv(in_channels, hidden_channels,
                              1, stride=1, act=activation)
        self.spp0 = nn.MaxPool2d(
            kernel_size=kernel_sizes[0], stride=1, padding=kernel_sizes[0] // 2)
        self.spp1 = nn.MaxPool2d(
            kernel_size=kernel_sizes[1], stride=1, padding=kernel_sizes[1] // 2)
        self.spp2 = nn.MaxPool2d(
            kernel_size=kernel_sizes[2], stride=1, padding=kernel_sizes[2] // 2)
        conv2_channels = hidden_channels * (len(kernel_sizes) + 1)
        self.conv2 = BaseConv(conv2_channels, out_channels,
                              1, stride=1, act=activation)

    def forward(self, x):
        x = self.conv1(x)
        spp0 = self.spp0(x)
        spp1 = self.spp1(spp0)
        spp2 = self.spp2(spp1)
        if self.qat:
            x = self.qat_func.cat([x, spp0, spp1, spp2], dim=1)
        else:
            x = torch.cat([x, spp0, spp1, spp2], dim=1)
        x = self.conv2(x)
        return x


class CSPLayer(nn.Module):
    """C3 in yolov5, CSP Bottleneck with 3 convolutions"""

    def __init__(
        self,
        in_channels,
        out_channels,
        n=1,
        shortcut=True,
        expansion=0.5,
        depthwise=False,
        act="silu",
        qat=False
    ):
        """
        Args:
            in_channels (int): input channels.
            out_channels (int): output channels.
            n (int): number of Bottlenecks. Default value: 1.
        """
        # ch_in, ch_out, number, shortcut, groups, expansion
        super().__init__()
        self.qat = qat

        hidden_channels = int(out_channels * expansion)  # hidden channels
        self.conv1 = BaseConv(
            in_channels, hidden_channels, 1, stride=1, act=act)
        self.conv2 = BaseConv(
            in_channels, hidden_channels, 1, stride=1, act=act)
        self.conv3 = BaseConv(2 * hidden_channels,
                              out_channels, 1, stride=1, act=act)
        module_list = [
            Bottleneck(
                hidden_channels, hidden_channels, shortcut, 1.0, depthwise, act=act, qat=qat
            )
            for _ in range(n)
        ]
        self.m = nn.Sequential(*module_list)
        self.qat_func = nn.quantized.FloatFunctional()

    def forward(self, x):
        x_1 = self.conv1(x)
        x_2 = self.conv2(x)
        x_1 = self.m(x_1)
        x = self.qat_func.cat((x_1, x_2), dim=1)
        return self.conv3(x)


class Focus(nn.Module):
    """Focus width and height information into channel space."""

    def __init__(self, in_channels, out_channels, ksize=1, stride=1, act="silu", qat=False):
        super().__init__()
        self.conv = BaseConv(in_channels * 4, out_channels,
                             ksize, stride, act=act)
        self.qat = qat
        self.qat_func = nn.quantized.FloatFunctional()

    def forward(self, x):
        # shape of x (b,c,w,h) -> y(b,4c,w/2,h/2)
        patch_top_left = x[..., ::2, ::2]
        patch_top_right = x[..., ::2, 1::2]
        patch_bot_left = x[..., 1::2, ::2]
        patch_bot_right = x[..., 1::2, 1::2]
        if self.qat:
            x = self.qat_func.cat(
                (
                    patch_top_left,
                    patch_bot_left,
                    patch_top_right,
                    patch_bot_right,
                ),
                dim=1,
            )
        else:
            x = torch.cat(
                (
                    patch_top_left,
                    patch_bot_left,
                    patch_top_right,
                    patch_bot_right,
                ),
                dim=1,
            )
        return self.conv(x)
