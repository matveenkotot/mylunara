from PIL import Image, ImageFilter

def blur_region(img, x1, y1, x2, y2, radius=18):
    region = img.crop((x1, y1, x2, y2))
    blurred = region.filter(ImageFilter.GaussianBlur(radius))
    img.paste(blurred, (x1, y1))
    return img

# screen1.png — скрин совместимости
# блюрим "(05.02.1997, Москва)" в тексте бота
img = Image.open("screen1.png")
img = blur_region(img, 33, 1700, 600, 1810)
img.save("screen1_safe.png")
print("screen1_safe.png готов")

# screen2.png — скрин с возвращением (оффер)
# блюрим "05.02.1997" в "Твоя карта на 05.02.1997 (Москва)"
img = Image.open("screen2.png")
img = blur_region(img, 33, 240, 500, 320)
img.save("screen2_safe.png")
print("screen2_safe.png готов")

# screen3.png — скрин онбординга (ввод даты)
# блюрим "05.02.1997 12:15 Москва" в сообщении пользователя
img = Image.open("screen3.png")
img = blur_region(img, 245, 1420, 890, 1550)
img.save("screen3_safe.png")
print("screen3_safe.png готов")
