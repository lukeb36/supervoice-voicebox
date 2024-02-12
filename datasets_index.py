import torch
import torchaudio
import multiprocessing
from glob import glob
from tqdm import tqdm

def get_duration(path):
    audio, sr = torchaudio.load(path)
    return path, len(audio[0]) / sr

def main():

    # Enumerate files
    print("Enumerating files...")
    files = []
    files += glob("datasets/common-voice-en-prepared/*/*.wav")
    files += glob("datasets/common-voice-ru-prepared/*/*.wav")
    files += glob("datasets/common-voice-uk-prepared/*/*.wav")

    # Calculate duration
    print("Calculating durations...")
    durations = []
    with multiprocessing.Manager() as manager:
        files = manager.list(files)
        with multiprocessing.Pool(processes=8) as pool:
            for result in tqdm(pool.imap_unordered(get_duration, files, chunksize=32), total=len(files)):
                path, duration = result
                durations.append((path, duration))

    # Writing file list
    print("Writing file list...")
    sorted_files = sorted(durations, key=lambda x: (-x[1], x[0]))
    with open("./datasets/list_pretrain.csv", "w") as filelist:
        for file in sorted_files:
            filelist.write(file[0] + "," + str(file[1]) + "\n")
                

if __name__ == "__main__":
    main()