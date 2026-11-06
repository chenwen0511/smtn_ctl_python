# 货架灯串口控制（新版测试 / 无写号）

对应厂家 `reelshelfTEST.exe` 的 **新版测试** 页：主板 **352**，储位 **7 位直接用**。

**现场已验证**：`python shelf_light.py on 3520890` 可点亮该储位红灯。

## 准备

树莓派（推荐，HTTP 服务）：

```bash
./start.sh start      # 自动创建 .venv、安装依赖，串口 CH340 by-id，HTTP :8000
```

启停与接口见 [接口文档.md](接口文档.md)。

Windows 命令行点灯：

```bat
pip install -r requirements.txt
```

项目根目录若已有 `.venv`，不必 activate（PowerShell 可能禁脚本），直接：

```bat
cd /d D:\04-work\13-HUAWEI-SMT\01-doc\苏州-品立科技-PTL
.\.venv\Scripts\python.exe ctl_python\shelf_light.py on 3520890
```

1. 关掉「料架测试」里的红色 **关闭**（必须释放串口，否则 `PermissionError: 拒绝访问`）
2. S050 已供电，A/B 已接到灯板
3. 树莓派串口为 `/dev/ttyUSB0`（CH340），Windows 多为 `COM3`

## 命令

```bat
cd /d D:\04-work\13-HUAWEI-SMT\01-doc\苏州-品立科技-PTL\ctl_python

python shelf_light.py on 3520890
python shelf_light.py off 3520890
python shelf_light.py allon 352
python shelf_light.py alloff 352
python shelf_light.py tower 352
python shelf_light.py tower-off 352

# 批量操作多个灯
python shelf_light.py multi-on 3520890 3520891 3520892   # 批量点亮
python shelf_light.py multi-off 3520890 3520891 3520892  # 批量熄灭
```

颜色、换口：

```bat
python shelf_light.py on 3520890 -c G
python shelf_light.py on 3520890 -p COM3 -b 38400
python shelf_light.py multi-on 3520890 3520891 -c B      # 批量蓝色
```

## 灯塔

灯塔协议与储位灯不同（来自厂家 DLL `LightHouse_IC` 实测），颜色编码为 `00=R / 01=G / 02=B`，**不要**套用储位灯的 `21/11/01`：

```bat
python shelf_light.py -c R tower 352      # 灯塔红色常亮
python shelf_light.py -c G tower 352      # 灯塔绿色常亮
python shelf_light.py -c B tower 352      # 灯塔蓝色常亮
python shelf_light.py tower-off 352       # 灯塔灭
```

报文格式：`AA 54 + 主板(2字节) + 00 00 + 颜色 + 状态(00灭/01亮/11闪) + 88`

主板 352 各颜色常亮应打印：

```text
R:  AA 54 01 60 00 00 00 01 88
G:  AA 54 01 60 00 00 01 01 88
B:  AA 54 01 60 00 00 02 01 88
灭: AA 54 01 60 00 00 00 00 88
```

## HTTP 接口

树莓派上：

```bash
./start.sh start         # 默认 CH340 by-id，HTTP :8000
./start.sh stop
./start.sh status
./start.sh enable-boot   # systemd 开机自启
```

完整接口说明见 [接口文档.md](接口文档.md)。

只看报文、不发串口：

```bat
python shelf_light.py on 3520890 --dry-run
```

`3520890` 红灯常亮应打印：

```text
AA 55 01 60 21 03 DD 00 FF 88
```
