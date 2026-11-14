## Abstract

Convolutional feature channels can carry interdependent information, so their relative responses matter for image classification. Stacked convolutional transformations learn these interactions implicitly, motivating explicit channel recalibration to emphasize useful features and suppress less useful ones. We compare residual networks (ResNets) with matched-depth SE-ResNets using the ImageNet-1K results reported by Hu et al.; each SE-ResNet incorporates squeeze-and-excitation (SE) blocks that use global average pooling and learned channel gates to scale feature responses. Under a shared training setting and single-scale, single-crop validation, SE-ResNet lowers top-1 error by 0.79–1.51 percentage points relative to matched-depth ResNet baselines across all three evaluated depths on ImageNet-1K, with small increases in forward-pass computation. Top-5 error also decreases at every matched depth. The comparisons show that channel recalibration improves classification accuracy across the evaluated ResNet depths while adding little forward-pass computation.

## 1 Introduction

### 1.1 Channel Dependencies in Image Classification

Convolutional feature channels can carry interdependent information, such as responses to edges and textures that together describe an image. A channel’s value for classification can therefore depend on the responses of other channels. Convolutional networks learn these interactions implicitly through their stacked transformations, motivating an explicit mechanism for channel recalibration.

Channel recalibration assigns weights to feature responses channel by channel. The aim is to emphasize useful features and suppress less useful ones in light of the information represented across channels. Hu et al. introduced squeeze-and-excitation (SE) blocks, which use global channel summaries to learn channel gates \cite{hu2018squeeze}, making recalibration responsive to each image.

Residual networks (ResNets), which use shortcut connections to support residual learning \cite{he2016resnet}, provide the backbone for assessing channel recalibration. Comparing a ResNet with its SE-augmented counterpart at the same depth connects the channel-dependency motivation to a controlled architectural comparison.

### 1.2 Contributions

The ImageNet-1K comparison reported by Hu et al. \cite{hu2018squeeze} supports two findings about SE-ResNet, a ResNet augmented with SE blocks.

The matched-depth comparison shows lower top-1 and top-5 error for SE-ResNet-50, SE-ResNet-101, and SE-ResNet-152 than for their respective ResNet baselines under the same training setting. Both error metrics decrease at every matched depth, as shown in Section 5.

The computational-cost assessment shows that these error reductions come with small increases in forward-pass computation. The increase holds across all three matched-depth comparisons in Section 5.

## 2 Related Work

### 2.1 Residual Learning and Channel Recalibration

ResNet provides the backbone for the comparison, building on residual learning with identity shortcuts \cite{he2016resnet}. Hu et al. introduced the SE block to model channel dependencies explicitly through channel recalibration \cite{hu2018squeeze}.

### 2.2 Channel and Spatial Attention

The present comparison focuses on SE channel attention, whereas the Convolutional Block Attention Module (CBAM) also incorporates spatial attention \cite{woo2018cbam}.

## 3 Method

### 3.1 Squeeze: Global Channel Descriptors

The SE block uses global channel statistics to recalibrate feature responses. We adopt the SE block of Hu et al. \cite{hu2018squeeze}, beginning with global average pooling over each feature channel.

Let $U\in\mathbb{R}^{C\times H\times W}$ be the input feature tensor to the SE block, where $C$ is the number of channels and $H$ and $W$ are the feature-map height and width. For channel index $c$ and spatial indices $i$ and $j$, $u_c(i,j)$ denotes the feature value at position $(i,j)$. The channel descriptor $z\in\mathbb{R}^{C}$ contains one component $z_c$ for each channel:

$$
z_c=\frac{1}{HW}\sum_{i=1}^{H}\sum_{j=1}^{W}u_c(i,j).
$$

Each descriptor component averages one channel over all spatial positions. Together, the components organize global feature-map statistics by channel and serve as input to channel gating.

### 3.2 Excitation and Channel Scaling

Excitation converts the channel descriptor into gates that determine the relative weighting of feature channels. Each gate can depend on statistics from multiple channels, allowing the block to model nonlinear channel dependencies.

The excitation operation uses two fully connected layers with a rectified linear unit (ReLU) between them and a sigmoid gate at the output. Let $r$ be the reduction ratio, with default value $r=16$. The first layer has weights $W_1\in\mathbb{R}^{(C/r)\times C}$ and reduces the descriptor dimension from $C$ to $C/r$. The second layer has weights $W_2\in\mathbb{R}^{C\times(C/r)}$ and restores the channel dimension. The resulting channel gate vector $s\in\mathbb{R}^{C}$ is

$$
s=\sigma\!\left(W_2\delta(W_1z)\right),
$$

where $\delta$ denotes ReLU and $\sigma$ denotes the sigmoid function. The reduction ratio controls the bottleneck width; the default choice balances accuracy and parameter count.

The first layer combines descriptor components in the bottleneck, and ReLU introduces a nonlinearity before the second layer computes the channel gates. A gate can therefore depend on the global statistics of multiple channels. The two layers learn how those statistics contribute to each gate, producing weights conditioned on the input feature tensor.

The sigmoid gate allows multiple channels to receive high weights simultaneously. Each component $s_c$ lies between zero and one, so the gates adjust the relative prominence of channels. Channels assigned higher weights retain more of their responses, while channels assigned lower weights are suppressed more strongly.

Channel-wise scaling applies these gates to the input features. Let $\widetilde X$ denote the recalibrated feature tensor and $\widetilde X_c$ its channel $c$. The scaling operation is

$$
\widetilde X=s\odot U,
\qquad
\widetilde X_c=s_c\cdot u_c,
$$

where $\odot$ denotes channel-wise multiplication with spatial broadcasting. Each scalar gate $s_c$ multiplies every spatial position in its corresponding channel. The output $\widetilde X$ retains the spatial dimensions and channel organization of $U$, with responses recalibrated according to the learned gates.

### 3.3 Integration into Residual Networks

We use ResNet backbones with shortcut connections \cite{he2016resnet} and insert an SE block into each residual branch. Each branch first transforms its input into residual features, which the SE block then recalibrates. The recalibrated features are added to the shortcut output to produce the block output.

This placement makes channel recalibration operate on the residual features before they are combined with the shortcut. For an identity shortcut, the identity mapping carries the branch input directly to the addition. At dimension changes, the shortcut uses a projection to match the dimensions required for addition.

The same integration rule defines each SE-ResNet variant from its corresponding backbone. SE-ResNet-50 incorporates SE blocks into the residual branches of ResNet-50, SE-ResNet-101 uses ResNet-101, and SE-ResNet-152 uses ResNet-152. Each pair shares its backbone depth, with the SE variant adding channel recalibration to the residual branches. The training objective is [DESIGN DETAIL NEEDED].

## 4 Experimental Setup

### 4.1 Dataset and Evaluation Protocol

The comparison uses ImageNet-2012, the ImageNet-1K classification dataset, with 1,000 classes [CITATION NEEDED]. Its training split contains approximately 1.28 million images, and its validation split contains 50,000 images.

Validation uses a single-scale, single-crop protocol. Each image is resized so that its shorter side is 256 pixels, then a 224 × 224 center crop is used for prediction.

Top-1 error is the fraction of validation images for which the highest-scoring predicted class differs from the correct class. Top-5 error is the fraction for which the correct class is absent from the five highest-scoring predictions. Lower values indicate fewer classification errors for both metrics. Computational cost is reported in GFLOPs for one forward pass with a 224 × 224 input.

### 4.2 Training and Model Comparison

The comparison pairs ResNet-50, ResNet-101, and ResNet-152 \cite{he2016resnet} with SE-ResNet-50, SE-ResNet-101, and SE-ResNet-152, respectively. The SE-ResNet models use SE blocks \cite{hu2018squeeze} with $r=16$. The comparison uses results reported by Hu et al., including baseline reimplementations trained under the shared settings \cite{hu2018squeeze}.

Training images undergo random crops sampled by scale and aspect ratio, then resized to 224 × 224 pixels. The protocol also applies random horizontal flips and red, green, and blue (RGB) channel mean subtraction.

All models train from scratch for 100 epochs using synchronous distributed stochastic gradient descent (SGD). Training uses a mini-batch size of 1,024, momentum of 0.9, and an initial learning rate of 0.6. The learning rate decreases by a factor of 10 every 30 epochs. Weights use the He et al. initialization strategy [CITATION NEEDED].

## 5 Results

### 5.1 Classification Error Across Backbone Depths

SE-ResNet lowered both top-1 and top-5 error at every matched depth on ImageNet-1K. The comparisons reported by Hu et al. \cite{hu2018squeeze} paired each SE-ResNet with its corresponding ResNet baseline \cite{he2016resnet} under the same training setup. Table 1 presents the errors and forward-pass computation for all six models.

**Table 1. Matched-depth ImageNet-1K classification errors and forward-pass computation**

| Method | Top-1 Err ↓ | Top-5 Err ↓ | GFLOPs ↓ |
|---|---:|---:|---:|
| ResNet-50 | 24.80 | 7.48 | **3.86** |
| SE-ResNet-50 | 23.29 | 6.62 | 3.87 |
| ResNet-101 | 23.17 | 6.52 | 7.58 |
| SE-ResNet-101 | 22.38 | 6.07 | 7.60 |
| ResNet-152 | 22.42 | 6.34 | 11.30 |
| SE-ResNet-152 | **21.57** | **5.73** | 11.32 |

*Results reported by Hu et al. \cite{hu2018squeeze}.*

At depth 50, SE-ResNet-50 reduced top-1 error by 1.51 percentage points and top-5 error by 0.86 percentage points relative to ResNet-50. The top-1 reduction was the largest among the three matched-depth comparisons.

At depth 101, SE-ResNet-101 reduced top-1 error by 0.79 percentage points and top-5 error by 0.45 percentage points relative to ResNet-101. At depth 152, the corresponding reductions were 0.85 and 0.61 percentage points. The direction of the comparison was consistent across depths for both metrics, although the size of the reduction varied.

SE-ResNet-152 achieved the lowest errors among the listed models, with top-1 error of 21.57% and top-5 error of 5.73%. A cross-depth comparison also favored SE-ResNet-101 over ResNet-152: its top-1 error was 22.38% versus 22.42%, and its top-5 error was 6.07% versus 6.34%. In this comparison, a shallower SE-ResNet achieved lower error on both metrics than the deeper ResNet baseline.

### 5.2 Computational Cost of Recalibration

The error reductions came with small increases in forward-pass GFLOPs at every matched depth. As shown in Table 1, SE-ResNet-50 required 0.01 more GFLOPs than ResNet-50. The corresponding increases were 0.02 GFLOPs for the depth-101 pair and 0.02 GFLOPs for the depth-152 pair. The two deeper pairs had equal absolute increases, both larger than the increase for the depth-50 pair.

Relative to their matched ResNet baselines, the increases were 0.26%, 0.26%, and 0.18% at depths 50, 101, and 152, respectively. Although the absolute increases were equal for the two deeper pairs, the increase represented a smaller fraction of baseline computation at depth 152. Across all three pairs, SE-ResNet achieved lower classification error with a small addition to forward-pass computation.

## 6 Conclusion

Channel recalibration with SE blocks lowers ImageNet-1K top-1 and top-5 error across all three matched-depth ResNet comparisons, with small increases in forward-pass computation. The comparisons quantify the error–compute trade-off under a shared training setting.

SE-ResNet-50 gives the largest top-1 error reduction, at 1.51 percentage points, alongside a 0.86 percentage-point reduction in top-5 error. Its forward-pass cost increases by 0.01 GFLOPs relative to ResNet-50. The corresponding increases are 0.02 GFLOPs for SE-ResNet-101 and 0.02 GFLOPs for SE-ResNet-152. Across these depths, SE channel recalibration consistently lowers both error metrics at a small additional computational cost.

The figures come from the original paper rather than a new reproduction. The evaluation is limited to ImageNet-1K single-scale, single-crop classification.
