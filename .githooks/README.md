# 提交前检查

## 启用（每份 clone 一次）

    git config core.hooksPath .githooks

★ 这一步【不会自动发生】：`.git/config` 不进版本库，所以
  克隆下来必须手动设一次。没设的话钩子静默不生效 ——
  而「静默不生效」正是这个仓库反复栽的坑，所以在这里写明。

验证是否生效：

    git config core.hooksPath      # 应输出 .githooks

## 它检查什么

1. `python -m bili_digest.quality.invariants` —— 5 项静态不变量
   （换行符规则存在、状态集不写死、动作模块有 __main__、
     文本写带 newline、FTS kind 同源）
2. `python -m unittest discover -s tests` —— 12 项行为测试
   （失败分类、mark_gone 只标 gone、add_block 幂等、status_in 拒绝注入）

两项都在秒级。**这一点是硬约束**：钩子一旦让人等，人就会用
`--no-verify` 绕过，那它就成了摆设 —— 比没有更糟，因为它给人一种
「有检查」的错觉。

## 为什么放 .githooks/ 而不是 .git/hooks/

`.git/hooks/` 不进版本库（clone 下来只有 `.sample`），放在那里的钩子
只在本机存在，换机器就没了。用 `core.hooksPath` 指向仓库内目录，
钩子本身也被版本管理。

出处：safeguard.sh「Share Git hooks with your team using core.hooksPath」。

## 为什么是 #!/bin/sh

Windows 上写 `pre-commit.sh` 而不指定解释器是不会跑的。
`sh` 是 Git for Windows（MSYS2）自带的，`#!/bin/sh` 能被直接执行。

Python 解释器用绝对路径，因为 hook 运行时的 PATH 未必与登录 shell 一致；
可用环境变量 `KB_PYTHON` 覆盖。

## 确需跳过

    git commit --no-verify

但请先确认不是真问题 —— 这个仓库的历史说明，
「看起来在工作」比「明确失败」危险得多。
