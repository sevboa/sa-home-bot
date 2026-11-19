from aiogram.types import BufferedInputFile

b = BufferedInputFile(b"test", filename="test.txt")
print("Attributes:", dir(b))
