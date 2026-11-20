"""只裁本次输入的完整旧轮次，不总结、不改写任何历史文字。"""

import base64
import hashlib
import json
import math

from .image_policy import ImageCopies


def image_tokens(width, height, model):
    name = model.lower().rsplit("/", 1)[-1]
    if name == "gpt-4o-mini":
        return 2833 + 5667 * math.ceil(width / 512) * math.ceil(height / 512)
    if name == "gpt-4o":
        return 85 + 170 * math.ceil(width / 512) * math.ceil(height / 512)
    if name.startswith("gemini"):
        return 512 * math.ceil(width / 768) * math.ceil(height / 768)
    return 256 + math.ceil(width / 32) * math.ceil(height / 32) * (
        3 if name == "gpt-4.1-nano" else 2
    )


def wire_size(messages, system, output):
    # 默认ASCII转义与aiohttp/requests一致，再预留协议/模型/配置字段64KiB。
    body = {"messages": messages, "system": system, "max_tokens": output}
    return len(json.dumps(body, ensure_ascii=True).encode("utf-8")) + 65536


class ContextPlanner:
    def __init__(self):
        self.images = ImageCopies()

    def _message(self, row, policy, image_reader=None):
        role, content = row.get("role"), row.get("content")
        if role not in ("user", "assistant"):
            raise ValueError("答疑上下文角色无效")
        parts = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        if not isinstance(parts, list) or not parts:
            raise ValueError("答疑上下文正文无效")
        output, cost, pictures, hits = [], 32, 0, 0
        for part in parts:
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text = part["text"]
                output.append({"type": "text", "text": text})
                cost += len(text.encode("utf-8"))  # 字节上界估算，避免按汉字/4低估。
            elif part.get("type") in ("image_url", "local_image") and role == "user":
                if part["type"] == "local_image":
                    if image_reader is None:
                        raise ValueError("缺少本地截图读取器")
                    raw = image_reader(part["id"])
                    output_part, image_cost, image_count, cache_hits = self._message(
                        {
                            "role": role,
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/jpeg;base64,"
                                        + base64.b64encode(raw).decode("ascii")
                                    },
                                }
                            ],
                        },
                        policy,
                    )
                    output.extend(output_part["content"])
                    cost += image_cost - 32
                    pictures += image_count
                    hits += cache_hits
                    continue
                url = part.get("image_url", {}).get("url", "")
                if (
                    not isinstance(url, str)
                    or not url.startswith("data:image/")
                    or ";base64," not in url
                ):
                    raise ValueError("答疑图片必须是本地截图，禁止远程网址和文件路径")
                try:
                    raw = base64.b64decode(url.split(";base64,", 1)[1], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("答疑图片编码无效") from exc
                copy = self.images.prepare(raw, policy)
                encoded = base64.b64encode(copy["data"]).decode("ascii")
                output.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + encoded},
                    }
                )
                cost += image_tokens(
                    copy["width"], copy["height"], policy.get("model", "")
                )
                pictures += 1
                hits += copy["cache_hit"]
            else:
                raise ValueError("答疑只接受文字和截图")
        if pictures > 63:
            raise ValueError("当前问题截图超过63张，请分成多次补充")
        body = (
            output[0]["text"]
            if len(output) == 1 and output[0]["type"] == "text"
            else output
        )
        return {"role": role, "content": body}, cost, pictures, hits

    def prepare(self, messages, system, policy, *, image_reader=None):
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("答疑请求必须以当前用户问题结束")
        if not isinstance(system, str) or len(system) > 64000:
            raise ValueError("答疑人格正文过长，请调整人设")
        rounds = []
        for row in messages:
            if row.get("role") == "user":
                rounds.append([])
            if not rounds:
                raise ValueError("历史缺少对应用户问题，不能发送孤立回复")
            rounds[-1].append(row)
        reserve = policy["output_tokens"]
        safety = max(
            256, math.ceil(policy["context_tokens"] * policy["safety_percent"] / 100)
        )
        available = min(
            policy["context_tokens"] - reserve - safety,
            (policy.get("input_limit") or policy["context_tokens"]) - safety,
        )
        system_cost = len(system.encode("utf-8")) + 128
        kept, total, pictures, hits = [], system_cost, 0, 0
        selected_rounds = 0
        for group in reversed(rounds):
            # 老轮次数量已经超限时不再解码久远图片。
            if len(kept) + len(group) > policy["max_messages"]:
                if not kept:
                    raise ValueError("当前问题消息数量超限，请减少内容")
                break
            lower_bound = sum(
                32
                + sum(
                    len(p.get("text", "").encode("utf-8"))
                    for p in (
                        [{"text": row["content"]}]
                        if isinstance(row["content"], str)
                        else row["content"]
                    )
                    if p.get("type", "text") == "text"
                )
                for row in group
            )
            if kept and total + lower_bound > available:
                break
            converted = [self._message(row, policy, image_reader) for row in group]
            candidate = [r[0] for r in converted] + kept
            cost = sum(r[1] for r in converted)
            size = wire_size(candidate, system, reserve)
            if total + cost > available or size > policy["max_bytes"]:
                if not kept:
                    raise ValueError(
                        "当前问题及截图独自已超过Token或10MiB预算；请减少截图、缩小框选区域或核对模型上下文配置。内容已保留，未调用模型。"
                    )
                break
            kept, total = candidate, total + cost
            pictures += sum(r[2] for r in converted)
            hits += sum(r[3] for r in converted)
            selected_rounds += 1
        stats = {
            "input_tokens_estimate": total,
            "output_tokens": reserve,
            "safety_tokens": safety,
            "context_tokens": policy["context_tokens"],
            "kept_rounds": selected_rounds,
            "dropped_rounds": len(rounds) - selected_rounds,
            "images": pictures,
            "image_cache_hits": hits,
            "wire_bytes": wire_size(kept, system, reserve),
            "source": policy["source"],
        }
        prefix = json.dumps(
            {"system": system, "messages": kept},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "messages": kept,
            "stats": stats,
            "prefix_hash": hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        }
