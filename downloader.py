import yt_dlp

FFMPEG_PATH = "./ffmpeg-2026-06-29-git-de6bcf5c05-essentials_build/bin"


def download_single():
    url = input("\nEnter YouTube video URL: ").strip()

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": "Single Audio/%(title)s.%(ext)s",
        "ignoreerrors": True,
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("\n Download complete!")


def download_playlist():
    url = input("\nEnter YouTube playlist URL: ").strip()

    start = int(input("Start index: "))
    end = int(input("End index: "))

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": "Playlist Audio%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": start,
        "playlistend": end,
        "ignoreerrors": True,
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("\n Playlist download complete!")

def download_video():
    url = input("\nEnter YouTube video URL: ").strip()

    ydl_opts = {
        # Best video + best audio
         "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        
        # Merge into MP4
        "merge_output_format": "mp4",

        # Save inside Videos folder
        "outtmpl": "Single Videos/%(title)s.%(ext)s",

        "ignoreerrors": False,
        "verbose":True,
        "ffmpeg_location": FFMPEG_PATH,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("\n✅ Video download complete!")

def download_playlist_video():
    url = input("\nEnter YouTube playlist URL: ").strip()

    start = int(input("Start index: "))
    end = int(input("End index: "))

    ydl_opts = {
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": "Playlist Videos/%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": start,
        "playlistend": end,
        "ignoreerrors": False,
        "verbose":True,
        "ffmpeg_location": FFMPEG_PATH,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("\nPlaylist video download complete!")

def main():
    while True:
        print("\n========== YouTube MP3 Downloader ==========")
        print("1. Download Single Audio")
        print("2. Download Playlist Audio")
        print("3. Download Single Video")
        print("4. Download Playlist Video")
        print("5. Exit")

        choice = input("\nChoose an option (1-4): ").strip()

        if choice == "1":
            download_single()

        elif choice == "2":
            download_playlist()

        elif choice == "3":
            download_video()

        elif choice == "4":
            download_playlist_video()

        elif choice == "5":
            print("Goodbye!")
            break

        else:
            print(" Invalid choice. Please try again.")

if __name__ == "__main__":
    main()