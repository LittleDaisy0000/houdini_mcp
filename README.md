# houdini_mcp
本项目旨在利用 AI 通过 Houdini MCP（Model Context Protocol）生态，深度辅助开发者在 Houdini 中的创作流程。目前处于早期开发阶段，功能持续完善中。

一.环境配置

安装 uv;

https://github.com/astral-sh/uv

二.在 MoBu里运行接收端脚本

在 Houdini 的 Python 控制台执行下面脚本，把receiver_path改成本机houdini_receiver_template.py的路径;

    import importlib.util, traceback
    p = r"F:\houdini_mcp\houdini_receiver_template.py"
    spec = importlib.util.spec_from_file_location("houdini_receiver_live", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    print("Loaded:", m.__file__, "version:", m.RECEIVER_VERSION)
    try:
        m.start_receiver()
        print("Receiver start requested on", m.HOST, m.PORT)
    except Exception:
        traceback.print_exc()

三.启用MCP并检验

在 Claude，Cursor，其他AI编程客户端里启用houdini_mcp后，在聊天栏调用 houdini_health确认连通。
