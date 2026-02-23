"""
docdetect.py — Multi-document detection using Apple Vision framework.

Strategy:
1. VNDetectDocumentSegmentationRequest gates the pipeline (any document present?)
2. VNDetectRectanglesRequest finds multiple candidate rectangles
3. Each candidate is validated by running document segmentation on the crop
4. Falls back to the single segmentation result if rectangle detection doesn't help
"""

import tempfile, os
from PIL import Image


def detect_and_extract_documents(pil_img, max_documents=10):
    """Detect and perspective-correct multiple documents in an image.

    Returns list of PIL Images sorted left-to-right by center position.
    """
    import numpy as np
    import cv2
    import Vision
    import Quartz
    from Foundation import NSURL

    w, h = pil_img.size

    # Write to temp file for Vision framework
    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
    try:
        pil_img.save(tmp.name, 'JPEG', quality=95)
        url = NSURL.fileURLWithPath_(tmp.name)
        ci_image = Quartz.CIImage.imageWithContentsOfURL_(url)
    finally:
        os.unlink(tmp.name)

    if ci_image is None:
        return []

    # Step 1: Gate — is there any document in this image?
    gate_obs = _detect_document_segmentation(ci_image)
    if gate_obs is None:
        return []

    # Step 2: Try rectangle detection for multiple documents
    rect_obs = _detect_rectangles(ci_image, max_documents)
    rect_obs = _filter_overlapping(rect_obs)

    # Step 3: Perspective-correct each rectangle
    cv_img = np.array(pil_img)
    if len(rect_obs) >= 2:
        documents = []
        for obs in rect_obs:
            if obs.confidence() < 0.5:
                continue
            warped = _perspective_correct(obs, cv_img, w, h)
            if warped is not None:
                cx = (obs.topLeft().x + obs.topRight().x) / 2
                documents.append((cx, Image.fromarray(warped)))

        if len(documents) >= 2:
            documents.sort(key=lambda pair: pair[0])
            return [img for _, img in documents]

    # Step 4: Fallback — use the single document segmentation result
    warped = _perspective_correct(gate_obs, cv_img, w, h)
    if warped is not None:
        return [Image.fromarray(warped)]
    return []


# ── Private helpers ───────────────────────────────────────────────────────────

def _detect_document_segmentation(ci_image):
    """Run document segmentation, return single observation or None."""
    import Vision

    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
    request = Vision.VNDetectDocumentSegmentationRequest.alloc().init()
    success, error = handler.performRequests_error_([request], None)
    if not success:
        return None
    results = request.results()
    if not results or len(results) == 0:
        return None
    obs = results[0]
    return obs if obs.confidence() >= 0.5 else None


def _detect_rectangles(ci_image, max_obs):
    """Run rectangle detection, return list of observations."""
    import Vision

    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
    request = Vision.VNDetectRectanglesRequest.alloc().init()
    request.setMaximumObservations_(max_obs)
    request.setMinimumAspectRatio_(0.3)
    request.setMaximumAspectRatio_(3.0)
    request.setMinimumSize_(0.1)
    request.setMinimumConfidence_(0.5)
    request.setQuadratureTolerance_(30.0)
    success, error = handler.performRequests_error_([request], None)
    if not success:
        return []
    results = request.results()
    return list(results) if results else []



def _perspective_correct(obs, cv_img, img_w, img_h):
    """Perspective-correct a single observation. Returns numpy array or None."""
    import numpy as np
    import cv2

    tl = obs.topLeft()
    tr = obs.topRight()
    br = obs.bottomRight()
    bl = obs.bottomLeft()

    corners = np.array([
        [tl.x * img_w, (1 - tl.y) * img_h],
        [tr.x * img_w, (1 - tr.y) * img_h],
        [br.x * img_w, (1 - br.y) * img_h],
        [bl.x * img_w, (1 - bl.y) * img_h],
    ], dtype=np.float32)

    width = int(max(np.linalg.norm(corners[1] - corners[0]),
                    np.linalg.norm(corners[2] - corners[3])))
    height = int(max(np.linalg.norm(corners[3] - corners[0]),
                     np.linalg.norm(corners[2] - corners[1])))

    if width < 50 or height < 50:
        return None

    dst = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(cv_img, M, (width, height))


def _filter_overlapping(observations, iou_threshold=0.5):
    """Remove overlapping detections, keeping higher-confidence ones."""
    if len(observations) <= 1:
        return observations
    sorted_obs = sorted(observations, key=lambda o: o.confidence(), reverse=True)
    kept = []
    for obs in sorted_obs:
        box = _obs_to_box(obs)
        if not any(_iou(box, _obs_to_box(k)) > iou_threshold for k in kept):
            kept.append(obs)
    return kept


def _obs_to_box(obs):
    """Return (min_x, min_y, max_x, max_y) from observation corners."""
    xs = [obs.topLeft().x, obs.topRight().x, obs.bottomRight().x, obs.bottomLeft().x]
    ys = [obs.topLeft().y, obs.topRight().y, obs.bottomRight().y, obs.bottomLeft().y]
    return (min(xs), min(ys), max(xs), max(ys))


def _iou(box1, box2):
    """Intersection over union of two axis-aligned bounding boxes."""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0
