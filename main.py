private void RunHeadPeakWithFullAnticheat(CancellationToken token)
{
    var memory = new VN();
    if (!memory.DatTienTrinh(new[] { "HD-Player" })) return;
    ApplyFullAnticheat(memory, token);
    BeepControl.SafeBeep(900, 120);
    bool enableNoise = true;
    int noiseReadsPerAddr = 4;
    int noiseRange = 0x70;
    bool noiseOnlyWhenShooting = true;
    int shootKeyCode = 0x01;
    bool checkShootKey = true;
    Random rand = new Random();
    DateTime lastDummyTime = DateTime.Now;
    string aimPattern = "01 00 00 00 00 00 00 00 ?? ?? ?? ?? 01 00 00 00 00 00 00 00 ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? 00 00 00 00 ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ?? ?? ?? ?? ?? ?? ?? ?? 00 00 00 00 ?? ?? ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ?? ?? ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 80 BF 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 00 00 ?? ?? ?? ??";
    var addrList = memory.QuetMauBoNho(aimPattern).Result.ToList();
    if (addrList.Count == 0) return; 
    while (!token.IsCancellationRequested)
    {
        if (!memory.DatTienTrinh(new[] { "HD-Player" })) break; 
        if (rand.Next(0, 200) == 0) 
        {
            Thread.Sleep(rand.Next(300, 1200)); 
            continue;
        }

        foreach (long addr in addrList)
        {
            token.ThrowIfCancellationRequested();
            bool isShooting = !checkShootKey || (GetAsyncKeyState(shootKeyCode) & 0x8000) != 0;

            if (isShooting)
            {
          
                if (_ignoreKnocked)
                {
                    try
                    {
    
                        string deadHex = memory.DocChuoi(addr + 0x50, 1);
                        deadHex = deadHex.Replace(" ", "").Replace("-", "").Trim();
                        if (!string.IsNullOrEmpty(deadHex))
                        {
           
                            if (Convert.ToInt32(deadHex, 16) != 0) continue;
                        }
                    }
                    catch { }
                }

                if (_aimDelay > 0) Thread.Sleep(_aimDelay);
                if (rand.Next(0, 100) < 60) Thread.Sleep(rand.Next(2, 8));
                if (rand.Next(0, 100) < 7) continue; // bỏ qua 7% frame

                try
                {
          
                    string headData = memory.DocChuoi(addr + 0x468, 4);
                    memory.ThayTheMau(addr + 0x14, headData);

    
                    if (enableNoise && (!noiseOnlyWhenShooting || isShooting))
                    {
                        for (int i = 0; i < noiseReadsPerAddr; i++)
                        {
                            long noiseOffset = addr + rand.Next(-noiseRange, noiseRange + 1);
                            memory.DocChuoi(noiseOffset, rand.Next(1, 10));
                        }
            
                        if (rand.Next(0, 100) < 30)
                        {
                            long extraNoise = addr + rand.Next(-0x100, 0x100);
                            memory.DocChuoi(extraNoise, rand.Next(1, 8));
                        }
                    }
                }
                catch { }
            }

   
            if ((DateTime.Now - lastDummyTime).TotalSeconds > rand.Next(6, 15))
            {
                try
                {
    
                    long safeAddr = addrList[rand.Next(addrList.Count)] + rand.Next(-0x50, 0x50);
                    memory.DocChuoi(safeAddr, rand.Next(4, 12));
                    lastDummyTime = DateTime.Now;
                }
                catch { }
            }

            Thread.Sleep(6 + rand.Next(0, 14));
        }
    }
}
