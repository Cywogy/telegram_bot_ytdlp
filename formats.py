"""Подбор одиночного файла или пары H.264 + AAC для сборки в MP4."""


def candidates(info, ceiling, limit):
    formats = [f for f in (info.get("formats") or [info])
               if f.get("format_id") and not f.get("has_drm")]
    audio = [f for f in reversed(formats) if f.get("vcodec") == "none"
             and (f.get("acodec") or "").startswith("mp4a")]
    usable = []
    for video in reversed(formats):
        if video.get("vcodec") == "none" or video.get("ext") == "mhtml":
            continue
        height = video.get("height") or 0
        quality = min(height, video.get("width") or height)
        if quality > ceiling or (video.get("filesize") or 0) > limit:
            continue
        candidate = dict(video, height=quality)
        if video.get("acodec") == "none":
            if not (video.get("vcodec") or "").startswith(("avc1", "h264")):
                continue
            track = next((a for a in audio if (video.get("filesize") or 0)
                          + (a.get("filesize") or 0) <= limit), None)
            if track is None:
                continue
            candidate["format_id"] += "+" + track["format_id"]
            candidate["filesize_approx"] = sum(
                f.get("filesize") or f.get("filesize_approx") or 0 for f in (video, track)
            )
        usable.append(candidate)
    return sorted(usable, key=lambda f: (
        (f.get("filesize_approx") or 0) > limit,
        -f["height"],
        "+" in f["format_id"],
    ))
