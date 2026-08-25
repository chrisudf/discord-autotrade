# 把整晚终端日志里"同一条记录刷 N 遍"的段落折叠掉，其余原样保留。
#
# [8/13 + 8/14] 连续两晚，一条 naked-short 拒单循环分别刷了 1918 / 812 组
# （每组 4 行）。8/13 那晚日志 8183 行 / 1.09 MB，前一晚只有 363 行 —— 复盘
# 光把日志读一遍就吃掉大半预算，而那 5754 行里真正的信息量是**一句话**。
#
# 归并的单位是**整条记录**，不是单行 —— 这一点是踩过才改对的：
# Discord 消息的第一行是 `📩 [频道] 作者: @everyone`，在 26 条内容完全不同的
# 消息里逐字相同，按行归并会把 24 条真实信号连同正文一起压掉，而"漏掉的信号"
# 恰恰是复盘第一优先级。所以一条记录 = 一个带时间戳的行 + 其后所有续行，
# 归并键由整条记录算出。
#
# 折叠规则：
#   - 归并键 = 记录全文去掉时间戳、数字换成 #、取前 200 字符。
#     这样 `last=8.40` 与 `last=14.80` 算同一条，不同的错误各自成组。
#   - 同键出现次数 > THRESH（默认 20）时：只留**首次**和**末次**两条完整记录，
#     中间打一行 `⋮ 共 N 次`。首末都留是有意的 —— 复盘要判断这个循环
#     什么时候开始、什么时候（被什么）停下，8/14 那晚正是靠末次时间戳
#     对上了 sync_positions 的落账时刻（00:52:57 拒单 → 00:53:00 落账）。
#   - 安静的夜晚（没有任何模式超过 THRESH）输出与输入逐行相同，零信息损失。
#
# 用法（两遍扫同一个文件，第一遍计数第二遍输出）：
#   awk -v THRESH=20 -f ops/collapse_log.awk FILE FILE

function is_stamped(line) {
    return line ~ /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9][0-9][0-9] \| /
}

# 把缓冲区里的记录算成归并键
function rec_key(   i, s) {
    s = ""
    for (i = 1; i <= nbuf; i++) s = s "\n" buf[i]
    sub(/^\n[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9][0-9][0-9] \| /, "", s)
    gsub(/[0-9]+/, "#", s)
    return substr(s, 1, 200)
}

function emit(   i) {
    for (i = 1; i <= nbuf; i++) print buf[i]
}

# 记录结束：第一遍计数，第二遍决定输出
function close_rec(   k) {
    if (nbuf == 0) return
    if (!stamped_rec) {          # 文件头的 banner 之类，无键，原样保留
        if (!counting) emit()
        nbuf = 0
        return
    }
    k = rec_key()
    if (counting) {
        cnt[k]++
    } else {
        seen[k]++
        if (cnt[k] > THRESH) {
            if (seen[k] == 1) {
                emit()
                printf("    ⋮⋮⋮ 以上这条记录共出现 %d 次，中间 %d 次已折叠（下面是最后一次）\n",
                       cnt[k], cnt[k] - 2)
            } else if (seen[k] == cnt[k]) {
                emit()
            }
        } else {
            emit()
        }
    }
    nbuf = 0
}

BEGIN { if (THRESH == 0) THRESH = 20; nbuf = 0; counting = 1 }

FNR == 1 && NR != FNR { close_rec(); counting = 0; nbuf = 0 }

{
    if (is_stamped($0)) {
        close_rec()
        stamped_rec = 1
    }
    buf[++nbuf] = $0
}

END { close_rec() }
