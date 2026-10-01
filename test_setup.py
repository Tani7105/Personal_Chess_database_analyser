import chess, chess.engine

engine = chess.engine.SimpleEngine.popen_uci("stockfish")
board = chess.Board()
info = engine.analyse(board, chess.engine.Limit(depth=15))
print("Best opening move:", board.san(info["pv"][0]))
print("Eval:", info["score"].white())
engine.quit()