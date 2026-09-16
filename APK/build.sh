#!/usr/bin/env bash
# BeaconMFG APK 构建脚本（Windows / Git Bash）
# 用法: ./build.sh [debug|release|clean]
set -uo pipefail

APK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"
# ANDROID_HOME 必须是 **Windows 风格路径**（C:/Users/...）。Gradle 跑在 JVM 上，
# 认不了 Git Bash 的 MSYS 路径；而 $HOME 在 Git Bash 下恰好是 `/c/Users/xxx`，
# 直接拼就报 "SDK location not found ... set sdk.dir in local.properties" ——
# 报错只提 local.properties，看不出其实是路径风格问题，极易往错方向排查。
# 所以优先取 USERPROFILE（原生 Windows 路径）并把反斜杠换成正斜杠。
_WIN_HOME="$(cygpath -w "${USERPROFILE:-$HOME}" 2>/dev/null || echo "${USERPROFILE:-$HOME}")"
export ANDROID_HOME="${ANDROID_HOME:-${_WIN_HOME//\\//}/AppData/Local/Android/Sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
GRADLE_BIN="${GRADLE_BIN:-$HOME/.workbuddy/binaries/gradle/gradle-dist/gradle-9.7.1/bin/gradle}"

TARGET="${1:-debug}"

echo "== 环境 =="
echo "JAVA_HOME   = $JAVA_HOME"
echo "ANDROID_HOME= $ANDROID_HOME"
"$JAVA_HOME/bin/java.exe" -version 2>&1 | head -1
echo ""

cd "$APK_DIR" || exit 1

# 注意：--no-build-cache 是必须的（在 WorkBuddy 沙箱下）。
# Gradle 的本地构建缓存会做 .part → 正式的 重命名/删除，被沙箱拦截后报
# "java.io.IOException: 拒绝访问" / AccessDeniedException，构建在 dex 合并阶段失败。
# 关掉缓存只是慢一点，不影响产物正确性。脱离沙箱环境时可去掉该参数。
case "$TARGET" in
  clean)
    exec "$GRADLE_BIN" :app:clean --console=plain --no-build-cache
    ;;
  release)
    exec "$GRADLE_BIN" :app:assembleRelease --console=plain --stacktrace --no-build-cache
    ;;
  *)
    exec "$GRADLE_BIN" :app:assembleDebug --console=plain --stacktrace --no-build-cache
    ;;
esac
