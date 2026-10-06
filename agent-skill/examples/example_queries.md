# 示例查询（BeaconMFG 薄框架检索）

安装本技能后，下列命令即可直接运行。数据从公开 CDN 按需拉取，**无需 clone 仓库**，
每次只拉取命中的那部分。

## 1. 按国标码 + 城市找模具厂
```bash
python client_search.py --industry 3525 --city 宁波 --limit 5
```

## 2. 按采购词自动解析成国标码
```bash
python client_search.py --keyword "CNC加工" --city 深圳
# 解析：CNC加工 → 国标码 3484（机械零部件加工）
```

## 3. 工艺 / 材料收敛
```bash
python client_search.py --industry 3525 --city 宁波 --proc cnc_milling --mat 铝合金6061
```

## 4. 拉某家企业完整档案（L2）
```bash
python client_search.py --industry 3525 --detail CN-MFG-0001234
```

## 5. 离线模式（需指向本地仓库）
```bash
python client_search.py --industry 3525 --city 宁波 --local --repo /path/to/beacon-mfg
```

## 典型返回形态
```
- CN-MFG-0001234 宁波某某模具有限公司（宁波）
  国标 3525 模具制造 · 工艺 cnc_milling,edm · 凭证L2 · 有电话
```
