# Tailscale 访问与直连核验

两台手机安装官方 Tailscale 客户端，加入同一 Tailnet 并保持连接。接收端浏览器访问：

```text
http://<车内安卓手机的 Tailscale IPv4>:8081/
```

输入自己配置的录像访问码。Tailscale 登录和录像网页登录分别完成；不要将两者的凭据混用或写入 URL。此场景不需要配置退出节点。

## 网页能显示什么

网页可识别当前 HTTP 请求来自私网、局域网或转发入口，但暂时无法读取官方安卓客户端内部的实时 peer 连接类型。因此显示“Tailscale 私网”时，附带“直连／中继未核验”。

不能仅凭以下信息声称视频正在 P2P 直连：

- 访问了 `100.x` 地址；
- 视频能播放或延迟较低；
- 日志曾出现 direct；
- 设备保有一个 DERP 连接。

## 如何确认当时为直连

让两端处于不同网络，例如车内安卓使用移动网络，接收端使用家庭 Wi-Fi。接收端播放录像时，在安卓 Tailscale 中打开该接收设备详情，点击右上角波纹形的 Ping 图标，查看结果是否为 `Direct connection`。

只有目标设备的实时状态能确认该次测试的连接路径。网络变化后可能切换为中继。DERP 或其他中继同样属于 Tailscale 私网，但视频数据会经过中继服务；协调与发现服务也可能参与直连的建立。

此方式不需要购买自己的服务器，但受 Tailscale 服务条款、当前套餐及网络可达性约束。项目目前未自动化安装客户端或管理账户。

## 资料

- [官方连接类型说明](https://tailscale.com/docs/reference/connection-types)
- [Android 客户端安装](https://tailscale.com/docs/install/android)
- [Android 进程内 LocalAPI](https://github.com/tailscale/tailscale-android/blob/main/libtailscale/localapi.go)

未来若官方提供支持的只读状态接口，再接入当前查看设备的实时连接类型；不可用猜测替代。
