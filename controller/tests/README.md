# 控制器回归

在仓库根目录运行：

```sh
python -B controller/run_tests.py
```

默认主机子集 58 次执行。Windows 上原生 flock 用例会跳过，其余使用内存协作者或临时文件；并发用例有专用、有限的测试子进程。Windows byte-range 锁替身不证明 Android flock/OFD 继承。

Android/Termux 原生套件：

```sh
python -B controller/run_tests.py --native --json <包外全新结果文件>
```

需已有 Python 和系统 shell，命令本身不要求 Root。128 次执行，包含真实 `/proc` 路径夹具、原生有限持锁子进程和三个退役脚本的 shell 检查。测试只使用专用临时目录或内存状态，不运行生产 USB、生产映像 fsck、业务服务或提权。

原生测试中的路径字符串/Android shell 假设不适用于 Windows；不要把完整原生套件在 Windows 上的夹具失败解释为生产代码回归。主机子集和 Android 既有结果分别记录。

发布副本另含六项 CLI/探测消费层检查，单独运行、单独统计：

```sh
python -B -m unittest discover -s controller/tests -p test_cli_sources.py
```

它用真正的私有 sys_block/mounts 文件和显式 Linux backing-file 身份，避免 Windows 盘符改变夹具语义；不观察真实 /sys/block 或 /proc/mounts。

`test_recovery_boundaries.py` 是内存协作者支持，已移除旧版“预期复现风险”的基线测试，不会把旧漏洞应该出现当成验收条件。`test_reassert.py` 是旧 CLI 风格用例，统一 runner 给它新建私有临时目录；不要手动传入生产目录。

controller/src 九模块保留已核验源码内容。测试目录重定位与支持路径修改属于发布适配，见 [验证范围](../../docs/VALIDATION-20261020.md)。
