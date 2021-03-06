import os
import random
import sys
from typing import Dict, Iterable, List

import cv2
import numpy as np
from tqdm import tqdm

sys.path.append('../')
sys.path.append('../src/')
sys.path.append('../src/modules/human_segmenter/')
sys.path.append('../src/modules/image_inpainter/')
sys.path.append('../src/modules/image_blender/')
sys.path.append('../src/modules/human_segmenter/checkpoints/')
from src.image_utils import dilate_bbox, dilate_mask, find_isolated_tracklets, get_shifted_bbox, \
    load_annotation_series, load_image_series, paste_masked_object, remove_mask, shrink_bbox, \
    update_and_save_annotation, get_bbox
from src.modules.human_segmenter import HumanSegmenter
from src.modules.image_blender import ImageBlender
from src.modules.image_inpainter import ImageInpainter

MOT_DATA_FOLDER = '../datasets/mot/train/'
#SEQ_FOLDER = 'MOT17-04'
SEQ_FOLDERS = ['MOT17-02', 'MOT17-04', 'MOT17-05', 'MOT17-09', 'MOT17-10', 'MOT17-11', 'MOT17-13']

OUTPUT_FOLDER = '../output/'
OUTPUT_VIDEO_FOLDER = '../output/video/'


def shift_trajectories(image_series: List[np.array],
                       annotation_series: List[Dict],
                       moving_objects_ids: Iterable[int],
                       video_writer: cv2.VideoWriter,
                       seq_folder: str):
    """
    Please note:
        This is incomplete at the moment.
        The code might not be performing the desired actions.

    Desired actions:
    For each human object selected as the `moving_objects`, we will
        "Destruction Stage"
            1. Perform human segmentation within the object's `bbox` -> get `mask`.
            Detail: in `human_segmenter.segment_image`,
                    perhaps set `bbox_outer` as dilated `bbox` and
                    `bbox_guess` as shrinked `bbox` for better segmentation.
            2. Remove the human object by clearing the `mask`.
            Detail: perhaps dilate `mask` for cleaner removal.
            3. Fill the missing vacancy of the removed human object using image inpainting.
        "Construction Stage"
            4. Calculate the human object's destintation after shifting.
            Shifting amount is a constant, pre-defined value for each object.
            Detail: need to handle edge cases such as out-of-boundary issues.
            5. Paste the segmented human object to the destination.
            6. Harmonize the pasted human object and its surroundings using image blending.
            7. Update the annotation series to reflect the updated bbox location.
    """

    # Human segmentation model
    human_segmenter = HumanSegmenter()
    # Image inpainting model
    image_inpainter = ImageInpainter()
    # Image blending model
    image_blender = ImageBlender()

    # Pre-define how much to shift each object.
    image_h, image_w = image_series[0].shape[:2]
    max_delta_h, max_delta_w = image_h / 2, image_w / 2
    # Hashmap: object id -> (x, y) shift for that object.
    shift_xy_by_object = dict([(k, (
        int(random.uniform(-max_delta_h, max_delta_h)),
        int(random.uniform(-max_delta_w, max_delta_w)),
    )) for k in sorted(moving_objects_ids)])

    # Hashmap: (image_id, track_id) -> bbox
    updated_bbox = {}

    for image_idx in tqdm(range(len(image_series))):
        adjusted_image = image_series[image_idx].copy()
        full_mask_inpainting = np.zeros_like(adjusted_image[:, :, 0])

        # "Destruction Stage"
        for ann in annotation_series:
            if ann['image_id'] - 1 != image_idx:
                continue
            if not ann['track_id'] in moving_objects_ids:
                continue

            orig_bbox = ann['bbox']
            xmin, xmax, ymin, ymax = get_bbox(adjusted_image, orig_bbox)
            full_mask_inpainting[xmin:xmax, ymin:ymax] = 255

            # Step 1. Perform human segmentation -> get mask.
            mask = human_segmenter.segment_image(
                image=image_series[image_idx],
                bbox_outer=dilate_bbox(orig_bbox),
                bbox_guess=shrink_bbox(orig_bbox))

            mask = dilate_mask(mask)
            full_mask_inpainting[mask > 0] = mask[mask > 0]

            # Step 2. Remove the human object by clearing the mask.
            remove_mask(image=adjusted_image, mask_to_remove=mask)

        # Step 3. Fill the missing vacancy of the removed human objects using image inpainting.
        adjusted_image = image_inpainter.inpaint_image(image=adjusted_image,
                                    mask=full_mask_inpainting)

        # "Construction Stage"
        # The second loop is used to avoid the `remove_mask` operation to affect pasted objects.
        for ann in annotation_series:
            if ann['image_id'] - 1 != image_idx:
                continue
            if not ann['track_id'] in moving_objects_ids:
                continue

            orig_bbox = ann['bbox']
            mask = human_segmenter.segment_image(
                image=image_series[image_idx],
                bbox_outer=dilate_bbox(orig_bbox),
                bbox_guess=shrink_bbox(orig_bbox))

            # Step 4. Calculate the human object's destintation after shifting.
            shifted_bbox = get_shifted_bbox(
                bbox=orig_bbox,
                shift_xy=shift_xy_by_object[ann['track_id']],
                image_shape_xy=adjusted_image.shape[:2])

            # Step 5. Paste the segmented human object to the destination.
            adjusted_image, shifted_mask = paste_masked_object(
                background=adjusted_image,
                foreground=image_series[image_idx],
                orig_mask=mask,
                shift_xy=shift_xy_by_object[ann['track_id']])

            # Step 6. Harmonize the pasted human object and its surroundings using image blending.
            adjusted_image = image_blender.blend_image(
                image=adjusted_image,
                mask=dilate_mask(shifted_mask),
                bbox_fov=dilate_bbox(shifted_bbox))

            # Step 7. Update the annotation series to reflect the updated bbox location.
            assert '%s-%s' % (ann['image_id'],
                              ann['track_id']) not in updated_bbox.keys()
            updated_bbox['%s-%s' %
                         (ann['image_id'], ann['track_id'])] = shifted_bbox

        # Save image
        adjusted_image = cv2.cvtColor(adjusted_image, cv2.COLOR_RGB2BGR)
        cv2.imwrite(
            OUTPUT_IMAGE_FOLDER + str(image_idx + 1).zfill(6) + ".jpg",
            adjusted_image)

        # Collect image into video stream
        if video_writer is not None:
            video_writer.write(adjusted_image)

    update_and_save_annotation(
        updated_bbox,
        output_folder=OUTPUT_ANNOTATION_FOLDER,
        mot_data_folder=MOT_DATA_FOLDER,
        seq=seq_folder,
    )

    return video_writer


if __name__ == '__main__':
    random.seed(0)

    for SEQ_FOLDER in SEQ_FOLDERS:

        OUTPUT_IMAGE_FOLDER = OUTPUT_FOLDER + '%s_alt-GT/img1/' % SEQ_FOLDER
        OUTPUT_ANNOTATION_FOLDER = OUTPUT_FOLDER + '%s_alt-GT/gt/' % SEQ_FOLDER
        for folder in [
                OUTPUT_IMAGE_FOLDER, OUTPUT_ANNOTATION_FOLDER, OUTPUT_VIDEO_FOLDER
        ]:
            os.makedirs(folder, exist_ok=True)

        num_frames = None  # To parse all frames, use `None`.
        image_series = load_image_series(MOT_DATA_FOLDER,
                                         seq=SEQ_FOLDER,
                                         first_k=num_frames)
        annotation_series = load_annotation_series(MOT_DATA_FOLDER,
                                                   seq=SEQ_FOLDER,
                                                   first_k=num_frames)

        isolated_tracklets = find_isolated_tracklets(annotation_series,
                                                     first_k=num_frames)

        video_writer = cv2.VideoWriter(OUTPUT_VIDEO_FOLDER +
                                       '%s_shift_trajectories.mp4' % SEQ_FOLDER,
                                       cv2.VideoWriter_fourcc(*'mp4v'),
                                       fps=30,
                                       frameSize=image_series[0].shape[:2][::-1])

        video_writer = shift_trajectories(
            image_series=image_series,
            annotation_series=annotation_series,
            moving_objects_ids=isolated_tracklets,
            video_writer=video_writer,
            seq_folder=SEQ_FOLDER
        )

    video_writer.release()
