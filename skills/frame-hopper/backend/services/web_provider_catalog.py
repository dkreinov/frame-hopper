"""Dated, research-backed website discovery; never a live credit balance.

Model access describes the advertised site and plan, not an authenticated form.
The browser API must still inspect the current account before a generation.
"""

from __future__ import annotations

from dataclasses import dataclass


REVIEWED_ON = "2026-10-02"
FREE_POSSIBLE = {"confirmed", "conditional"}


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    hosts: tuple[str, ...]
    media: tuple[str, ...]
    models: dict[str, str]
    free_offer: str
    cadence: str
    source: str
    browser_support: str = "navigation_only"
    limitation: str = ""


PROVIDERS = (
    Provider("kling", "https://kling.ai/app/", ("kling.ai", "klingai.com"),
             ("video",), {"kling": "conditional"},
             "Limited-time promotional credits; daily login credits depend on an active event.",
             "conditional", "https://kling.ai/docs/point-policy", "guarded_video_job"),
    Provider("dreamina", "https://dreamina.capcut.com/ai-tool/generate?type=video",
             ("dreamina.capcut.com",), ("video",), {"seedance": "confirmed"},
             "Daily free credits for Seedance and first/last-frame video.", "daily",
             "https://dreamina.capcut.com/resource/first-last-frame", "guarded_video_job"),
    Provider("pixverse", "https://app.pixverse.ai/", ("pixverse.ai",),
             ("video",), {"pixverse": "confirmed", "kling": "paid", "seedance": "paid"},
             "Basic: 60 initial and 30 daily credits for eligible PixVerse models; Kling O3 is on higher plans.",
             "daily", "https://app.pixverse.ai/subscribe", "guarded_video_job"),
    Provider("vidu", "https://www.vidu.com/create/img2video", ("vidu.com",),
             ("video",), {"vidu": "confirmed"},
             "Signup bonus and daily login credits; exact amount is account dependent.", "daily",
             "https://s.vidu.com/pricing", "guarded_video_job"),
    Provider("flow", "https://flow.google.com/", ("flow.google.com",),
             ("video",), {"veo": "confirmed", "gemini-omni": "unverified"},
             "50 daily credits without a subscription; free-credit model restrictions and peak-hour limits apply.",
             "daily", "https://support.google.com/flow/answer/16526234?hl=en", "guarded_video_job"),
    Provider("krea", "https://www.krea.ai/video", ("krea.ai",),
             ("video",), {"kling": "paid", "seedance": "paid", "veo": "paid", "runway": "paid"},
             "100 daily free compute units cover basic image tools; video models require a paid plan.",
             "daily_images_only", "https://www.krea.ai/blog/best-free-ai-video-generators-in-2026",
             "guarded_video_job"),
    Provider("seaart", "https://www.seaart.ai/", ("seaart.ai",),
             ("video",), {"seaart": "unverified"},
             "Free credit and model terms require a current account check.", "unverified",
             "https://www.seaart.ai/", "guarded_video_job"),
    Provider("openart", "https://openart.ai/home", ("openart.ai",),
             ("video",), {"kling": "unverified", "seedance": "unverified", "veo": "unverified"},
             "40 one-time trial credits expire after 7 days; no ongoing free usage plan.",
             "one_time", "https://openart.ai/blog/what-is-openart/", "guarded_video_job",
             "Trial credits may be below the cost of a video generation."),
    Provider("hailuo", "https://hailuoai.video/create/image-to-video", ("hailuoai.video",),
             ("video",), {"hailuo": "conditional"},
             "Free basic mode and one-time welcome credits expiring after 3 days; no verified daily refill.",
             "one_time", "https://hailuoai.video/doc/payment-policy.html", "guarded_video_job"),
    Provider("runway", "https://app.runwayml.com/", ("runwayml.com",),
             ("video",), {"runway": "conditional"},
             "125 one-time free credits; check which current video model the free account allows.",
             "one_time", "https://help.runwayml.com/hc/en-us/articles/15124877443219-How-do-credits-work"),
    Provider("luma", "https://dream-machine.lumalabs.ai/", ("lumalabs.ai",),
             ("video", "audio"), {"ray": "confirmed", "kling": "unverified", "veo": "unverified",
                                    "elevenlabs": "unverified"},
             "Free Ray3.14 Draft Mode; other listed partner models and audio need account-level eligibility checks.",
             "limited", "https://lumalabs.ai/learning-hub/ray3-faq"),
    Provider("leonardo", "https://app.leonardo.ai/", ("leonardo.ai",),
             ("video",), {"leonardo-motion": "unverified"},
             "150 free tokens daily; verify video model and cost in the current account.",
             "daily", "https://www.leonardo.ai/pricing"),
    Provider("firefly", "https://firefly.adobe.com/", ("adobe.com",),
             ("video", "audio"), {"firefly-video": "confirmed", "firefly-audio": "confirmed",
                                     "ray": "confirmed", "kling": "unverified"},
             "Limited daily free generations across selected video and audio models; partner Kling eligibility is unverified.",
             "daily", "https://www.adobe.com/products/firefly/plans.html"),
    Provider("magnific", "https://www.freepik.com/ai/", ("freepik.com", "magnific.com"),
             ("video", "audio"), {"kling": "unverified"},
             "Free account exists; Kling 3.0 Omni is listed, but free-tier Kling access is unverified.",
             "unverified", "https://www.freepik.com/ai/docs/video-ai-models"),
    Provider("elevenlabs", "https://elevenlabs.io/app", ("elevenlabs.io",),
             ("audio",), {"elevenlabs": "confirmed"},
             "10,000 free credits monthly for text-to-speech, sound effects and music.",
             "monthly", "https://elevenlabs.io/pricing", limitation="Free output is for personal, noncommercial use."),
    Provider("cartesia", "https://www.cartesia.ai/", ("cartesia.ai",),
             ("audio",), {"cartesia": "confirmed"},
             "20,000 monthly free credits for Sonic text-to-speech and Ink speech-to-text.",
             "monthly", "https://www.cartesia.ai/pricing",
             limitation="Commercial-use license starts on a paid plan."),
    Provider("fish-audio", "https://fish.audio/", ("fish.audio",),
             ("audio",), {"fish-audio": "confirmed"},
             "8,000 monthly free credits, up to about 7 minutes of text-to-speech.",
             "monthly", "https://fish.audio/plan/",
             limitation="Free output is personal and noncommercial; 500 characters per generation."),
    Provider("minimax-audio", "https://www.minimax.io/audio/", ("minimax.io",),
             ("audio",), {"minimax-audio": "conditional"},
             "Free users may receive daily time-limited credits; the current amount is not specified.",
             "conditional", "https://www.minimax.io/audio/doc/paid-service-terms.html"),
    Provider("suno", "https://suno.com/create", ("suno.com",),
             ("audio",), {"suno": "confirmed"},
             "50 credits daily with the free v6-mini model.", "daily",
             "https://suno.com/pricing", limitation="Free plan lists no monthly song downloads or commercial rights."),
)

BY_NAME = {provider.name: provider for provider in PROVIDERS}
MODEL_FAMILIES = tuple(sorted({model for provider in PROVIDERS for model in provider.models}))
MODEL_MEDIA = {"elevenlabs": "audio", "firefly-audio": "audio", "suno": "audio",
               "cartesia": "audio", "fish-audio": "audio", "minimax-audio": "audio"}


def select_providers(*, model: str | None = None, media: str | None = None,
                     free_only: bool = False) -> tuple[Provider, ...]:
    """Filter by actual listed model family, then optional free-model eligibility."""
    model = model.strip().lower() if model else None
    media = media.strip().lower() if media else None
    if model and model not in MODEL_FAMILIES:
        raise ValueError(f"unknown model family: {model}")
    if media and media not in {"video", "audio"}:
        raise ValueError(f"unknown media: {media}")
    return tuple(provider for provider in PROVIDERS
                 if (not model or model in provider.models)
                 and (not media or media in provider.media)
                 and (not model or not media or MODEL_MEDIA.get(model, "video") == media)
                 and (not free_only or (
                     provider.models.get(model) in FREE_POSSIBLE if model
                     else any(access in FREE_POSSIBLE and (
                         not media or MODEL_MEDIA.get(family, "video") == media)
                         for family, access in provider.models.items())
                 )))
