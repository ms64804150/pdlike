# 版本号规则

正式发布从 1.0.0 开始，后续功能更新只递增中间位：

    1.0.0 -> 1.1.0 -> 1.2.0

发布下一版前，在项目根目录运行：

    powershell -ExecutionPolicy Bypass -File .\packaging\bump-minor-version.ps1

该脚本会保留主版本号，将次版本号加一，并将末位固定为 0。之后按正常流程提交、打包和发布。
