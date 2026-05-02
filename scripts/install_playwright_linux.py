#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playwright Linux 国内一键安装提速脚本
功能：自动配置国内镜像，加速 Playwright 及其浏览器的安装全过程。
适用：Ubuntu/Debian、CentOS/RHEL/Fedora、Arch 等主流 Linux 发行版。
"""

import os
import re
import shutil
import subprocess
import sys

CDN_MIRRORS = [
    "https://cdn.npmmirror.com/binaries/playwright",
    "https://npmmirror.com/mirrors/playwright",
    "https://cdn.playwright.dev",  # 官方 CDN 兜底
]

# Chromium 自 Playwright 1.57+ 改用 Chrome for Testing 构建，需要单独的镜像
CHROMIUM_CDN_MIRRORS = [
    "https://cdn.npmmirror.com/binaries/chrome-for-testing",
]


def run_command(cmd, error_msg, env=None, check=True):
    """执行 shell 命令，并处理错误"""
    try:
        subprocess.check_call(cmd, shell=True, env=env or os.environ)
    except subprocess.CalledProcessError as e:
        if check:
            print(f"[ERROR] {error_msg}")
            print(f"命令退出码：{e.returncode}")
            sys.exit(1)
        else:
            print(f"[WARN] {error_msg}")
            return False
    return True


def detect_distro():
    """检测 Linux 发行版"""
    distro = "unknown"
    if os.path.isfile("/etc/os-release"):
        with open("/etc/os-release") as f:
            content = f.read().lower()
            if "ubuntu" in content or "debian" in content:
                distro = "debian"
            elif "centos" in content or "rhel" in content or "red hat" in content:
                distro = "rhel"
            elif "fedora" in content:
                distro = "fedora"
            elif "arch" in content or "manjaro" in content:
                distro = "arch"
            elif "suse" in content or "opensuse" in content:
                distro = "suse"
    print(f"[INFO] 检测到发行版类型：{distro}")
    return distro


def check_sudo():
    """检查是否有 sudo 权限（安装系统依赖时需要）"""
    if os.geteuid() == 0:
        return True
    if shutil.which("sudo"):
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        # 有 sudo 但需要密码，仍然可用
        return True
    return False


def install_system_deps(distro):
    """安装 Playwright 浏览器运行所需的系统级依赖"""
    print("[STEP] 检查系统依赖...")

    has_sudo = check_sudo()
    prefix = "sudo " if os.geteuid() != 0 else ""

    if not has_sudo and os.geteuid() != 0:
        print("[WARN] 无 sudo 权限，跳过系统依赖安装。")
        print("       如果浏览器安装后无法运行，请以 root 权限重新执行此脚本，")
        print("       或手动执行: python -m playwright install-deps")
        return

    # playwright install --with-deps 会自动处理系统依赖，
    # 但我们先确保基础工具链存在
    base_pkgs = {
        "debian": f"{prefix}apt-get update -qq && {prefix}apt-get install -y -qq curl wget",
        "rhel": f"{prefix}yum install -y -q curl wget",
        "fedora": f"{prefix}dnf install -y -q curl wget",
        "arch": f"{prefix}pacman -Sy --noconfirm --needed curl wget",
        "suse": f"{prefix}zypper install -y curl wget",
    }

    cmd = base_pkgs.get(distro)
    if cmd:
        run_command(cmd, "基础工具安装失败（curl/wget）", check=False)
        print("[OK] 基础工具就绪。")
    else:
        print("[WARN] 未识别的发行版，跳过基础工具检查。")


def install_playwright():
    """使用国内镜像源安装 Playwright 核心库"""
    print("[STEP] 步骤1：使用清华源安装 Playwright 库...")
    pip_cmd = (
        f"{sys.executable} -m pip install playwright "
        f"-i https://pypi.tuna.tsinghua.edu.cn/simple "
        f"--trusted-host pypi.tuna.tsinghua.edu.cn"
    )
    run_command(pip_cmd, "Playwright 库安装失败，请检查网络或 pip 版本。")
    print("[OK] Playwright 库安装成功！")


def find_config_file():
    """
    自动查找 Playwright 的 CDN 配置文件 index.js。
    Linux 下典型路径：
      ~/.local/lib/python3.X/site-packages/playwright/driver/package/lib/server/registry/index.js
      /usr/lib/python3/dist-packages/playwright/driver/package/lib/server/registry/index.js
      venv/lib/python3.X/site-packages/playwright/driver/package/lib/server/registry/index.js
    """
    import site

    potential_paths = []

    # 策略0：通过 playwright 命令获取驱动路径
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "--path"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            driver_path = result.stdout.strip()
            potential_paths.append(
                os.path.join(driver_path, "package", "lib", "server", "registry", "index.js")
            )
    except Exception:
        pass

    # 策略1：site-packages
    for sp_dir in site.getsitepackages():
        potential_paths.append(
            os.path.join(
                sp_dir, "playwright", "driver", "package", "lib", "server", "registry", "index.js"
            )
        )

    # 策略2：用户 site-packages
    try:
        user_sp = site.getusersitepackages()
        potential_paths.append(
            os.path.join(
                user_sp, "playwright", "driver", "package", "lib", "server", "registry", "index.js"
            )
        )
    except Exception:
        pass

    # 策略3：当前 venv
    if sys.prefix != sys.base_prefix:
        venv_path = os.path.join(
            sys.prefix,
            "lib",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
            "site-packages",
            "playwright",
            "driver",
            "package",
            "lib",
            "server",
            "registry",
            "index.js",
        )
        potential_paths.insert(0, venv_path)

    # 策略4：find 命令兜底
    try:
        result = subprocess.run(
            [
                "find",
                sys.prefix,
                "-path",
                "*/playwright/driver/package/lib/server/registry/index.js",
                "-type",
                "f",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if line and line not in potential_paths:
                    potential_paths.append(line)
    except Exception:
        pass

    return potential_paths


def patch_config_file():
    """查找并修补 Playwright 的 CDN 配置文件"""
    print("[STEP] 步骤2：定位并修补 Playwright 浏览器下载配置文件...")

    potential_paths = find_config_file()
    target_file = None
    for path in potential_paths:
        if os.path.isfile(path):
            target_file = path
            break

    if not target_file:
        print("[WARN] 未能自动定位配置文件。")
        print("       请手动查找 index.js，通常位于：")
        print("       <site-packages>/playwright/driver/package/lib/server/registry/index.js")
        print("       找到后将 PLAYWRIGHT_CDN_MIRRORS 数组替换为多镜像回退配置。")
        return False

    print(f"[OK] 找到配置文件：{target_file}")

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 匹配 const PLAYWRIGHT_CDN_MIRRORS = [ ... ];
        pattern = r"(const\s+PLAYWRIGHT_CDN_MIRRORS\s*=\s*\[)[^\]]*(\]\s*;)"
        match = re.search(pattern, content)

        if not match:
            print("[WARN] 未找到 PLAYWRIGHT_CDN_MIRRORS 定义，可能版本有变化。")
            print("       建议手动检查文件内容。")
            return False

        old_mirrors = match.group(0)
        mirrors_str = ", ".join(f"'{m}'" for m in CDN_MIRRORS)
        new_mirrors = match.group(1) + mirrors_str + match.group(2)

        if old_mirrors == new_mirrors:
            print("[OK] 配置文件已经是国内镜像，无需修改。")
            return True

        content_new = content.replace(old_mirrors, new_mirrors)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content_new)

        print("[OK] 成功将 CDN 镜像替换为国内淘宝源！")
        print(f"     旧配置: {old_mirrors[:100]}...")
        print(f"     新配置: {new_mirrors}")
        return True

    except PermissionError:
        print(f"[ERROR] 无写入权限：{target_file}")
        print(f"        请尝试: sudo python3 {__file__}")
        return False
    except Exception as e:
        print(f"[ERROR] 修改配置文件时出错：{e}")
        return False


def install_browsers(browsers=None):
    """安装浏览器，默认安装 chromium。支持多镜像自动回退。"""
    if browsers is None:
        browsers = ["chromium"]

    for browser in browsers:
        installed = False
        for i, mirror in enumerate(CDN_MIRRORS):
            label = "国内镜像" if i < len(CDN_MIRRORS) - 1 else "官方 CDN"
            print(f"[STEP] 步骤3：尝试从{label}安装 {browser} 浏览器...")
            print(f"       镜像地址：{mirror}")

            env = os.environ.copy()
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = mirror

            # Chromium 自 Playwright 1.57+ 使用 Chrome for Testing，需要单独的镜像
            if browser == "chromium" and CHROMIUM_CDN_MIRRORS:
                env["PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST"] = CHROMIUM_CDN_MIRRORS[0]
                print(f"       Chromium 镜像：{CHROMIUM_CDN_MIRRORS[0]}")

            install_cmd = f"{sys.executable} -m playwright install {browser} --with-deps"
            success = run_command(
                install_cmd, f"{browser} 从 {mirror} 安装失败。", env=env, check=False
            )
            if success:
                print(f"[OK] {browser} 浏览器安装完成！（来源：{label}）")
                installed = True
                break
            else:
                if i < len(CDN_MIRRORS) - 1:
                    print("[INFO] 当前镜像不可用，尝试下一个...")

        if not installed:
            print(f"[ERROR] {browser} 所有镜像均安装失败。")
            sys.exit(1)


def verify_installation():
    """验证安装是否成功"""
    print("[STEP] 验证安装...")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from playwright.sync_api import sync_playwright; "
                "p = sync_playwright().start(); "
                "b = p.chromium.launch(); "
                "page = b.new_page(); "
                'page.goto("about:blank"); '
                'print("BROWSER_OK"); '
                "b.close(); p.stop()",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "BROWSER_OK" in result.stdout:
            print("[OK] Playwright 与 Chromium 运行正常！")
            return True
        else:
            print("[WARN] 验证未通过，可能缺少系统依赖。")
            if result.stderr:
                # 只打印最后几行错误信息
                err_lines = result.stderr.strip().splitlines()[-5:]
                for line in err_lines:
                    print(f"       {line}")
            print("       尝试执行: python -m playwright install-deps")
            return False
    except subprocess.TimeoutExpired:
        print("[WARN] 验证超时，但安装可能已成功。")
        return False
    except Exception as e:
        print(f"[WARN] 验证时出错：{e}")
        return False


def parse_args():
    """解析命令行参数"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Playwright Linux 国内一键安装提速脚本",
    )
    parser.add_argument(
        "--browsers",
        nargs="+",
        default=["chromium"],
        choices=["chromium", "firefox", "webkit"],
        help="要安装的浏览器列表（默认：chromium）",
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="跳过系统依赖安装",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="跳过安装验证",
    )
    parser.add_argument(
        "--patch-only",
        action="store_true",
        help="仅修补配置文件，不安装",
    )
    return parser.parse_args()


def main():
    print("=" * 60)
    print("  Playwright Linux 国内安装提速脚本 v1.0")
    print("=" * 60)

    if sys.version_info < (3, 7):
        print("[ERROR] 需要 Python 3.7 或更高版本。")
        sys.exit(1)

    if sys.platform != "linux":
        print(f"[WARN] 当前平台为 {sys.platform}，此脚本针对 Linux 优化。")
        print("       继续执行，但部分功能可能不适用。")

    args = parse_args()

    distro = detect_distro()

    # 安装系统依赖
    if not args.skip_deps and not args.patch_only:
        install_system_deps(distro)

    # 安装 Playwright 库
    if not args.patch_only:
        install_playwright()

    # 修补配置文件
    patched = patch_config_file()

    if args.patch_only:
        if patched:
            print("\n[DONE] 配置文件修补完成。")
        return

    # 安装浏览器
    if patched:
        install_browsers(args.browsers)
    else:
        print("[WARN] 配置文件未修补成功，尝试通过环境变量加速安装...")
        install_browsers(args.browsers)

    # 验证
    if not args.skip_verify and "chromium" in args.browsers:
        verify_installation()

    print()
    print("=" * 60)
    print("  安装完成！")
    print("=" * 60)
    print("  验证命令: python -m playwright --version")
    print("  如遇问题: python -m playwright install-deps")
    print("=" * 60)


if __name__ == "__main__":
    main()
