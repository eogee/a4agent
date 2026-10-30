"""任务下发与循环任务共用的技能校验 + 快照（方案 §8）。

从 tasks.py 提出的公共助手：`POST /tasks` 与 `POST/PUT /loops` 用同一份
校验口径（两层一个收一个拒都算口径分裂），失败统一 422、文案一致。
只做「名字非空 + 来源路径合法且可读」这类**零副作用**校验；不做试挂载
（试建再删会真的在用户项目里建目录，副作用大于收益），源在注入时再判一次。
"""
from pathlib import Path

from fastapi import HTTPException

from ... import skill_manager

MAX_SKILLS = 20  # 与 schemas.TaskCreate/LoopBase.skills 的 max_length 同值（方案 §4.2）


def skill_snapshot(skills: list, workdir: str = "") -> list:
    """校验所选技能并生成入库快照（全部在建行之前，失败回 422）。"""
    if not skills:
        return []
    if len(skills) > MAX_SKILLS:
        raise HTTPException(422, f"一次最多选择 {MAX_SKILLS} 个技能")
    # 两层必须同一口径：这里放行的来源，注入层（task_skills._known_roots）也必须认。
    # 注入层的根 = 全局 + 已注册项目 + **本任务工作目录**的项目级根，最后一项不在
    # all_known_skill_roots() 里（工作目录可能没登记进 projects.json），必须并上，
    # 否则工作目录里的项目级 skill 会被我们判 422 而注入层照收；复跑历史任务时
    # 快照里的 source_path 指向任务自己的工作目录，同理会被自己拒掉。
    known = [skill_manager._normkey(root) for root in skill_manager.all_known_skill_roots()]
    if workdir:
        try:
            known += [skill_manager._normkey(root)
                      for root in skill_manager.project_skill_roots(Path(workdir)).values()]
        except Exception:  # noqa: BLE001 - 目录异常时只是少放行，不能因此 500
            pass
    snapshot = []
    for ref in skills:
        name = (ref.name or "").strip()
        if not name:
            raise HTTPException(422, "技能名称不能为空")
        path_text = (ref.source_path or "").strip()
        dir_name = name
        if path_text:
            folder = Path(path_text)
            # 防任意路径：来源必须是某个已知 skill 根的直接子目录（方案 §8 第 2 条）
            if skill_manager._normkey(folder.parent) not in known:
                raise HTTPException(422, f"技能来源不在允许的 skill 目录内：{path_text}")
            info = skill_manager.read_skill(folder)
            if info is None:
                raise HTTPException(422, f"技能「{name}」已不存在，请重新选择")
            dir_name = str(info.get("dir_name") or folder.name)
        snapshot.append(
            {
                "name": name,
                "dir_name": dir_name,
                "source_tool": (ref.source_tool or "").strip(),
                "source_scope": (ref.source_scope or "").strip(),
                "source_project": (ref.source_project or "").strip(),
                "source_path": path_text,
            }
        )
    return snapshot
