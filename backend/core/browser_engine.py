import asyncio
import os
from contextlib import asynccontextmanager

_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": False,
    "i_know_what_im_doing": True,
    "firefox_user_prefs": {
        "layers.acceleration.disabled": True,
        "gfx.webrender.enabled": False,
        "gfx.webrender.all": False,
        "gfx.webrender.software": False,
        "gfx.canvas.azure.backends": "skia",
        "media.hardware-video-decoding.enabled": False,
    },
}


@asynccontextmanager
async def _new_browser():
    from camoufox.async_api import AsyncCamoufox

    opts = dict(_CAMOUFOX_OPTS)
    # 默认会尝试下载 uBlock Origin 扩展；若下载失败/未缓存会抛
    # InvalidAddonPath 导致浏览器无法启动。注册与验证并不需要该扩展，
    # 显式排除，避免因附加组件缺失而整体失败。
    try:
        from camoufox.addons import DefaultAddons
        opts["exclude_addons"] = [DefaultAddons.UBO]
    except Exception:
        pass

    async with AsyncCamoufox(**opts) as browser:
        yield browser


async def ensure_browser_installed():
    import subprocess
    import sys

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, "-m", "camoufox", "path"],
                capture_output=True,
                text=True,
                timeout=10,
            ),
        )
        cache_dir = result.stdout.strip()
        if cache_dir:
            exe_name = "camoufox.exe" if os.name == "nt" else "camoufox"
            exe_path = os.path.join(cache_dir, exe_name)
            if os.path.exists(exe_path):
                return
    except Exception:
        pass

    loop = asyncio.get_event_loop()

    def _do_install():
        from camoufox.pkgman import CamoufoxFetcher

        CamoufoxFetcher().install()

    await loop.run_in_executor(None, _do_install)
