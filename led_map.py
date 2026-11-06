# 储位灯序号 -> 协议灯号。
# 1..1000 与厂家 SingleLight_IC 表一致；1001..1400 按同一蛇形规律延伸。
# 偶数行正向、奇数行反向，每行 100 盏；序号 >700 时协议灯号 +100（跳过 700–799）。

MAX_LED = 1400


def encode_led_word(led: int) -> int:
    if not 1 <= led <= MAX_LED:
        raise ValueError("灯序号超出 1..%d: %s" % (MAX_LED, led))
    row, col = divmod(led - 1, 100)
    if row % 2 == 0:
        word = row * 100 + col
    else:
        word = (row + 1) * 100 - 1 - col
    if led > 700:
        word += 100
    return word
