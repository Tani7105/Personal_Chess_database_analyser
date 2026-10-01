import requests

USERNAME = "GodLike7105"
HEADERS = {"User-Agent": "chess-db personal project"}

def get_all_games(username):
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    archives = requests.get(url, headers=HEADERS).json()["archives"]

    games = []
    for month_url in archives:
        data = requests.get(month_url, headers=HEADERS).json()
        for g in data["games"]:
            if "pgn" in g:
                games.append(g)
    return games

if __name__ == "__main__":
    games = get_all_games(USERNAME)
    print(f"Fetched {len(games)} games")

    with open("data/my_games.pgn", "w") as f:
        for g in games:
            f.write(g["pgn"] + "\n\n")
    print("Saved to data/my_games.pgn")