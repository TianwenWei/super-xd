这是当前源码中模型提示词的纯 TXT 可读快照。
不是新设计，不改变提示词含义；固定文字原样保留，变量统一写成 {{变量名}}。

先看这三个主角色：
01-talker.txt             前台理解请求、回答、选择工具和委托。
03-vehicle-planner.txt    Pro 生成完整车控计划，不执行车辆。
04-thinker-research.txt   后台调研与推理。

其余模板：
02-persona.txt            表达风格。
05-thinker-quick.txt      后台简短查询的完整 system 文本。
06-speech-route.txt       语音首轮判断是否调用工具。
07-speech-chat.txt        已确定仅聊天或澄清后的答复生成。
08-conditional-messages.txt   当前运行时按条件另加的提示词。

配套说明：
09-variables-and-message-order.txt   变量含义和实际消息顺序。
10-fixed-variable-values.txt        技能索引及三种预设人设值。
manifest.json                       源码与导出文件的校验信息。

为什么可以固定成 TXT：模型接收的本来就是文本。把固定规则写在 Python 常量里只是当前实现方式，不是模型要求。
固定规则可以放 TXT，变量在发送前填入；用户输入、历史和工具回执仍需保留各自消息角色，工具 schema 独立传入。

本目录仅用于阅读。运行时仍从 Python 组装，并未改成读取这些 TXT。
这里没有真实用户对话、密钥或模型网络调用。
当前 Thinker 仍保留 load_skill → suggest_vehicle_action 的旧建议流程措辞，本次如实导出，没有修改运行行为。
