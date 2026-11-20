from PIL import Image
from collections import deque

def remove_white_background(img_path: str, out_path: str, tolerance: int = 25) -> bool:
    """
    Flood-fill BFS removes white background from borders,
    cropping to valid content bounding box.
    """
    try:
        im = Image.open(img_path).convert("RGBA")
        w, h = im.size
        px = im.load()
        visited = [False] * (w * h)
        q = deque()

        def is_white(r, g, b):
            return r >= 255 - tolerance and g >= 255 - tolerance and b >= 255 - tolerance

        # Enqueue borders
        for x in range(w):
            for y in [0, h - 1]:
                r, g, b, a = px[x, y]
                if is_white(r, g, b):
                    idx = y * w + x
                    visited[idx] = True
                    q.append((x, y))

        for y in range(h):
            for x in [0, w - 1]:
                idx = y * w + x
                if not visited[idx]:
                    r, g, b, a = px[x, y]
                    if is_white(r, g, b):
                        visited[idx] = True
                        q.append((x, y))

        while q:
            cx, cy = q.popleft()
            px[cx, cy] = (0, 0, 0, 0)
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < h:
                    nidx = ny * w + nx
                    if not visited[nidx]:
                        visited[nidx] = True
                        r, g, b, a = px[nx, ny]
                        if is_white(r, g, b):
                            q.append((nx, ny))

        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)

        im.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"[ImgUtils] Error removing white bg: {e}")
        return False
