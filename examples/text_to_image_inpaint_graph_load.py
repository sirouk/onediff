import time
import os
import gc
import shutil
import unittest
import tempfile
from PIL import Image

import numpy as np
import oneflow as flow
import oneflow as torch
flow.mock_torch.enable()

from utils import _cost_cnt
from diffusers import EulerDiscreteScheduler
from onediff import OneFlowStableDiffusionInpaintPipeline as StableDiffusionInpaintPipeline
from diffusers import utils

img_url = "https://raw.githubusercontent.com/CompVis/latent-diffusion/main/data/inpainting_examples/overture-creations-5sI6fQgYIuo.png"
mask_url = "https://raw.githubusercontent.com/CompVis/latent-diffusion/main/data/inpainting_examples/overture-creations-5sI6fQgYIuo_mask.png"
_model_id = "runwayml/stable-diffusion-inpainting"
_with_image_save = True


def _reset_session():
    # Close session to avoid the buffer name duplicate error.
    flow.framework.session_context.TryCloseDefaultSession()
    time.sleep(5)
    flow.framework.session_context.NewDefaultSession(flow._oneflow_global_unique_env)


def _test_sd_graph_save_and_load(is_save, graph_save_path, sch_file_path, pipe_file_path):
    if is_save:
        print("\n==> Try to run graph save...")
        _online_mode = False
        _pipe_from_file = False
    else:
        print("\n==> Try to run graph load...")
        _online_mode = True
        _pipe_from_file = True

    total_start_t = time.time()
    start_t = time.time()
    @_cost_cnt
    def get_pipe():
        if _pipe_from_file:
            scheduler = EulerDiscreteScheduler.from_pretrained(sch_file_path, subfolder="scheduler")
            sd_pipe = StableDiffusionInpaintPipeline.from_pretrained(
                pipe_file_path, scheduler=scheduler, revision="fp16", torch_dtype=torch.float16
                )
        else:
            scheduler = EulerDiscreteScheduler.from_pretrained(_model_id, subfolder="scheduler")
            sd_pipe = StableDiffusionInpaintPipeline.from_pretrained(
                _model_id, scheduler=scheduler, revision="fp16", torch_dtype=torch.float16
                )
        return scheduler, sd_pipe
    sch, pipe = get_pipe()
    
    @_cost_cnt
    def pipe_to_cuda():
        cu_pipe = pipe.to("cuda")
        return cu_pipe
    pipe = pipe_to_cuda()
    
    @_cost_cnt
    def config_graph():
        pipe.set_graph_compile_cache_size(9)
        pipe.enable_graph_share_mem()
    config_graph()
    
    if not _online_mode:
        pipe.enable_save_graph()
    else:
        @_cost_cnt
        def load_graph():
            assert (os.path.exists(graph_save_path) and os.path.isdir(graph_save_path))
            pipe.load_graph(graph_save_path, compile_unet=True, compile_vae=True)
        load_graph()
    end_t = time.time()
    print("sd init time ", end_t - start_t, 's.')
    
    @_cost_cnt
    def text_to_image(prompt, img, mask_img, image_size, num_images_per_prompt=1, prefix="", with_graph=False):
        if isinstance(image_size, int):
            image_height = image_size
            image_weight = image_size
        elif isinstance(image_size, (tuple, list)):
            assert len(image_size) == 2
            image_height, image_weight = image_size
        else:
            raise ValueError(f"invalie image_size {image_size}")
    
        cur_generator = torch.Generator("cuda").manual_seed(1024)
        images = pipe(
            prompt,
            height=image_height,
            width=image_weight,
            image=img,
            mask_image=mask_img,
            compile_unet=with_graph,
            compile_vae=with_graph,
            num_images_per_prompt=num_images_per_prompt,
            generator=cur_generator,
            output_type="np",
        ).images

        if _with_image_save:
            for i, image in enumerate(images):
                pipe.numpy_to_pil(image)[0].save(f"{prefix}{prompt}_{image_height}x{image_weight}_{i}-with_graph_{str(with_graph)}.png")

        return images
    
    sizes = (512,512)
    prompt = "Face of a yellow cat, high resolution, sitting on a park bench"
    img = utils.load_image(img_url).resize(sizes)
    mask_img =  utils.load_image(mask_url).resize(sizes)
    
    no_g_images = text_to_image(prompt, img, mask_img, sizes, prefix=f"is_save_{str(is_save)}-", with_graph=False)
    with_g_images = text_to_image(prompt, img, mask_img, sizes, prefix=f"is_save_{str(is_save)}-", with_graph=True)
    assert len(no_g_images) == len(with_g_images)
    for img_idx in range(len(no_g_images)):
        print("====> diff ", np.abs(no_g_images[img_idx] - with_g_images[img_idx]).mean())
        assert np.abs(no_g_images[img_idx] - with_g_images[img_idx]).mean() < 1e-2
    total_end_t = time.time()
    print("st init and run time ", total_end_t - total_start_t, 's.')

    @_cost_cnt
    def save_pipe_sch():
        pipe.save_pretrained(pipe_file_path)
        sch.save_pretrained(sch_file_path)
    
    @_cost_cnt
    def save_graph():
        assert os.path.exists(graph_save_path) and os.path.isdir(graph_save_path)
        pipe.save_graph(graph_save_path)
    
    if not _online_mode:
        save_pipe_sch()
        save_graph()

class OneFlowPipeLineGraphSaveLoadTests(unittest.TestCase):
    def tearDown(self):
        # clean up the VRAM after each test
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()
    
    def test_sd_graph_save_and_load(self):
        with tempfile.TemporaryDirectory() as f0:
            with tempfile.TemporaryDirectory() as f1:
                with tempfile.TemporaryDirectory() as f2:
                    _test_sd_graph_save_and_load(True, f0 ,f1, f2)
                    _reset_session()
                    _test_sd_graph_save_and_load(False, f0, f1, f2)

if __name__ == "__main__":
    unittest.main()