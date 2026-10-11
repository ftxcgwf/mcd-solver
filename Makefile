# 今天麦什么 —— 常用命令薄封装
#
# 只是把 scripts/ 下的脚本串成好记的名字，没有隐藏任何逻辑。
# Windows 用户直接用 python scripts/quickstart.py 即可，不必装 make。

PY ?= python3

.PHONY: help demo nutrition menu prices coupons test clean

help:
	@echo "今天麦什么 —— 麦当劳点单决策求解器"
	@echo ""
	@echo "  make demo       求解示例（需先完成价格校准）"
	@echo "  make nutrition  抓官网公开营养数据（无需 Token）"
	@echo "  make menu       抓 MCP 在售菜单与价格（需 Token）"
	@echo "  make prices     用 calculate-price 校准官方结算价（需 Token）"
	@echo "  make coupons    折算券后价（需 Token）"
	@echo "  make test       离线自检，不需要 Token"
	@echo "  make clean      清理 __pycache__"
	@echo ""
	@echo "  完整链路：nutrition -> menu -> prices -> coupons -> demo"

demo:
	$(PY) cli.py --budget 45 --max-kcal 1200 --persona all

nutrition:
	$(PY) scripts/fetch_nutrition_web.py

menu:
	$(PY) scripts/fetch_menu.py

prices:
	$(PY) scripts/verify_prices.py

coupons:
	$(PY) scripts/coupons.py --calibrate

test:
	$(PY) scripts/selftest.py

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	find . -name "*.pyc" -delete
