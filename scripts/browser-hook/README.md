# 网易云网页播放器请求嗅探脚本

用于研究网页播放器实际调用了哪些接口、带了哪些参数。给 NCM Studio 增加
新功能时，先在浏览器里点一遍，把请求导出来对照，比猜接口可靠。

## 安装

1. 安装 [Tampermonkey](https://www.tampermonkey.net/)（或 Violentmonkey）。
2. 新建脚本，把 `ncm-request-hook.user.js` 的内容整个粘进去，保存。
3. 打开 <https://music.163.com/st/webplayer>，右上角会出现「NCM Hook」面板。

脚本只匹配 `music.163.com`，不会注入别的站点。

## 能抓到什么

- `fetch`、`XMLHttpRequest`、`navigator.sendBeacon` 三种发请求的方式；
- 请求方法、路径、状态码、耗时；
- 响应 JSON（超长会截断，面板里可以展开看）；
- 点按钮后 3 秒内发出的请求会自动打上按钮文字标签，例如
  「收藏」「发布」「不感兴趣」。

`weapi` 的外层密钥每次随机，离线解不开，脚本只记录参数长度；`eapi`
用固定密钥，脚本会直接解出 `url`、`payload`、`header` 和摘要。

## 使用

面板上的按钮：

- **暂停 / 继续**：不再记录新请求（已经记下的保留）；
- **清空**：清空当前条目；
- **复制 / 下载**：导出 JSON，便于贴到别处分析；
- 过滤框按路径或标签筛选。

控制台也可以直接操作：

```js
__ncmHook.entries      // 当前记录
__ncmHook.export()     // 导出 JSON 字符串
__ncmHook.clear()
__ncmHook.pause(true)
__ncmHook.decodeEapiParams(hex)  // 手动解一段 eapi 参数
```

## 隐私

脚本只读页面自己发出的请求和响应，不读取 Cookie，也不会把数据发到任何
第三方。导出的 JSON 里可能含有账号相关的响应字段，分享前请自己检查。
