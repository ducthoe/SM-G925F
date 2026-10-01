/* RenderScript image operations expressed as portable OpenCL 1.2 kernels. */
#pragma OPENCL FP_CONTRACT OFF

__kernel void blur_y(__global const uchar *input, __global float *temporary,
                     __global const float *weights, uint width, uint height,
                     uint channels, uint pitch, int radius)
{
    uint x = get_global_id(0), y = get_global_id(1);
    for (uint c = 0; c < channels; c++) {
        float value = 0.0f;
        for (int d = -radius; d <= radius; d++) {
            uint row = clamp((int)y + d, 0, (int)height - 1);
            value += (float)input[row * pitch + x * channels + c] * weights[d + radius];
        }
        temporary[(y * width + x) * channels + c] = value;
    }
}

__kernel void blur_x(__global const float *temporary, __global uchar *output,
                     __global const float *weights, uint width, uint height,
                     uint channels, int radius)
{
    uint x = get_global_id(0), y = get_global_id(1);
    for (uint c = 0; c < channels; c++) {
        float value = 0.0f;
        for (int d = -radius; d <= radius; d++) {
            uint column = clamp((int)x + d, 0, (int)width - 1);
            value += temporary[(y * width + column) * channels + c] * weights[d + radius];
        }
        output[(y * width + x) * channels + c] = convert_uchar_sat_rtz(value);
    }
}
