//+------------------------------------------------------------------+
//|                                           Export_ML_Dataset.mq5 |
//|          Script Ekspor Fitur & Labeling Historis untuk ML/ONNX   |
//|               Mendukung XAUUSD / XAUUSD.vx (Semua Timeframe)     |
//+------------------------------------------------------------------+
#property copyright   "XAU Phase2 ML Prep"
#property link        ""
#property version     "1.00"
#property script_show_inputs

//--- Input Parameters
input string             InpSymbol        = "";       // Simbol (kosongkan = pakai chart, misal XAUUSD.vx)
input ENUM_TIMEFRAMES    InpTimeframe     = PERIOD_M15; // Timeframe data
input int                InpBarsToExport  = 10000;    // Jumlah Bar historis yang diekspor
input string             InpFileName      = "xauusd_ml_dataset.csv"; // Nama file output (di folder MQL5/Files)

//--- Parameter Indikator (sama dengan EA)
input int                InpEMA_Period    = 50;
input int                InpADX_Period    = 14;
input int                InpRSI_Period    = 14;
input int                InpATR_Period    = 14;
input double             InpATR_SL_Mult   = 2.0;      // Multiplier SL utk hitung label
input double             InpRR_Ratio      = 1.5;      // Target Risk:Reward utk label profit

//+------------------------------------------------------------------+
//| Script program start function                                    |
//+------------------------------------------------------------------+
void OnStart()
  {
   string sym = (InpSymbol != "") ? InpSymbol : _Symbol;
   
   Print("Memulai ekspor data ML untuk: ", sym, " Timeframe: ", EnumToString(InpTimeframe));
   
   //--- Buat handle indikator
   int h_ema = iMA(sym, InpTimeframe, InpEMA_Period, 0, MODE_EMA, PRICE_CLOSE);
   int h_adx = iADX(sym, InpTimeframe, InpADX_Period);
   int h_rsi = iRSI(sym, InpTimeframe, InpRSI_Period, PRICE_CLOSE);
   int h_atr = iATR(sym, InpTimeframe, InpATR_Period);
   
   if(h_ema==INVALID_HANDLE || h_adx==INVALID_HANDLE || h_rsi==INVALID_HANDLE || h_atr==INVALID_HANDLE)
     {
      Print("Gagal inisialisasi indikator.");
      return;
     }
   
   //--- Siapkan buffer rates & indikator
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied_rates = CopyRates(sym, InpTimeframe, 0, InpBarsToExport + 100, rates);
   if(copied_rates <= 200)
     {
      Print("Data historis tidak mencukupi (hanya didapat: ", copied_rates, " bar). Pastikan history chart sudah ter-download.");
      return;
     }
   
   double ema[], adx_main[], adx_pdi[], adx_mdi[], rsi[], atr[];
   ArraySetAsSeries(ema, true);
   ArraySetAsSeries(adx_main, true);
   ArraySetAsSeries(adx_pdi, true);
   ArraySetAsSeries(adx_mdi, true);
   ArraySetAsSeries(rsi, true);
   ArraySetAsSeries(atr, true);
   
   CopyBuffer(h_ema, 0, 0, copied_rates, ema);
   CopyBuffer(h_adx, 0, 0, copied_rates, adx_main);
   CopyBuffer(h_adx, 1, 0, copied_rates, adx_pdi);
   CopyBuffer(h_adx, 2, 0, copied_rates, adx_mdi);
   CopyBuffer(h_rsi, 0, 0, copied_rates, rsi);
   CopyBuffer(h_atr, 0, 0, copied_rates, atr);
   
   //--- Buka file CSV untuk ditulis
   int file_handle = FileOpen(InpFileName, FILE_WRITE|FILE_CSV|FILE_ANSI, ",");
   if(file_handle == INVALID_HANDLE)
     {
      Print("Gagal membuat file: ", InpFileName);
      return;
     }
   
   //--- Tulis Header CSV
   FileWrite(file_handle,
             "time",
             "open", "high", "low", "close", "tick_volume",
             "dist_to_ema_atr",
             "adx_main", "adx_pdi", "adx_mdi", "adx_di_diff",
             "rsi",
             "atr_normalized",
             "body_atr", "upper_shadow_atr", "lower_shadow_atr",
             "hour", "day_of_week",
             "signal_type", // 1 = Buy Candidate, 2 = Sell Candidate, 0 = None
             "label_win"    // 1 = Win (mencapai TP sebelum SL), 0 = Loss/No-Hit
            );
   
   int valid_rows = 0;
   int horizon = 30; // Evaluasi outcome dalam 30 bar ke depan
   
   // Loop bar dari yang paling lama ke yang terbaru (sisakan horizon bar terakhir)
   for(int i = copied_rates - 100; i >= horizon; i--)
     {
      double cur_close = rates[i].close;
      double cur_open  = rates[i].open;
      double cur_high  = rates[i].high;
      double cur_low   = rates[i].low;
      double cur_atr   = atr[i];
      if(cur_atr <= 0) continue;
      
      // Feature Engineering
      double dist_to_ema_atr   = (cur_close - ema[i]) / cur_atr;
      double adx_m             = adx_main[i];
      double adx_p             = adx_pdi[i];
      double adx_md            = adx_mdi[i];
      double adx_di_diff       = adx_p - adx_md;
      double cur_rsi           = rsi[i];
      double atr_norm          = cur_atr / cur_close * 1000.0;
      
      double body              = MathAbs(cur_close - cur_open) / cur_atr;
      double upper_shadow      = (cur_high - MathMax(cur_open, cur_close)) / cur_atr;
      double lower_shadow      = (MathMin(cur_open, cur_close) - cur_low) / cur_atr;
      
      MqlDateTime dt;
      TimeToStruct(rates[i].time, dt);
      int hour        = dt.hour;
      int day_of_week = dt.day_of_week;
      
      // Deteksi Tipe Sinyal Kandidat (Rule-based Fase 1)
      int signal_type = 0;
      // Follow Buy
      if(cur_close > ema[i] && adx_m >= 20 && adx_p > adx_md && cur_rsi < 70) signal_type = 1;
      // Follow Sell
      else if(cur_close < ema[i] && adx_m >= 20 && adx_md > adx_p && cur_rsi > 30) signal_type = 2;
      // Reversal Buy
      else if(cur_rsi <= 30 && lower_shadow >= 2.0 * body) signal_type = 1;
      // Reversal Sell
      else if(cur_rsi >= 70 && upper_shadow >= 2.0 * body) signal_type = 2;
      
      if(signal_type == 0) continue; // Hanya simpan bar yang memicu setup kandidat
      
      // Hitung Label Nyata (Outcome Simulasi TP / SL ke Depan)
      int label_win = 0;
      double sl_dist = InpATR_SL_Mult * cur_atr;
      double tp_dist = sl_dist * InpRR_Ratio;
      
      if(signal_type == 1) // Evaluasi Buy
        {
         double sl_price = cur_close - sl_dist;
         double tp_price = cur_close + tp_dist;
         
         for(int j = i - 1; j >= i - horizon; j--)
           {
            if(rates[j].low <= sl_price) { label_win = 0; break; } // Kena SL lebih dulu
            if(rates[j].high >= tp_price) { label_win = 1; break; } // Kena TP lebih dulu
           }
        }
      else if(signal_type == 2) // Evaluasi Sell
        {
         double sl_price = cur_close + sl_dist;
         double tp_price = cur_close - tp_dist;
         
         for(int j = i - 1; j >= i - horizon; j--)
           {
            if(rates[j].high >= sl_price) { label_win = 0; break; }
            if(rates[j].low <= tp_price) { label_win = 1; break; }
           }
        }
      
      // Tulis baris data ke CSV
      FileWrite(file_handle,
                TimeToString(rates[i].time, TIME_DATE|TIME_MINUTES),
                DoubleToString(cur_open, 3),
                DoubleToString(cur_high, 3),
                DoubleToString(cur_low, 3),
                DoubleToString(cur_close, 3),
                IntegerToString(rates[i].tick_volume),
                DoubleToString(dist_to_ema_atr, 4),
                DoubleToString(adx_m, 2),
                DoubleToString(adx_p, 2),
                DoubleToString(adx_md, 2),
                DoubleToString(adx_di_diff, 2),
                DoubleToString(cur_rsi, 2),
                DoubleToString(atr_norm, 4),
                DoubleToString(body, 4),
                DoubleToString(upper_shadow, 4),
                DoubleToString(lower_shadow, 4),
                IntegerToString(hour),
                IntegerToString(day_of_week),
                IntegerToString(signal_type),
                IntegerToString(label_win)
               );
      valid_rows++;
     }
   
   FileClose(file_handle);
   
   // Release handles
   IndicatorRelease(h_ema);
   IndicatorRelease(h_adx);
   IndicatorRelease(h_rsi);
   IndicatorRelease(h_atr);
   
   Print("Ekspor selesai! Berhasil menyimpan ", valid_rows, " baris data ke file: MQL5/Files/", InpFileName);
  }
//+------------------------------------------------------------------+
