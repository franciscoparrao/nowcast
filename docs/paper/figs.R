#!/usr/bin/env Rscript
# Publication figures for the nowcast C&G manuscript (run from repo root).
suppressMessages(library(ggplot2))
outdir <- "docs/paper/figs"; dir.create(outdir, showWarnings = FALSE, recursive = TRUE)

# Okabe-Ito colourblind-safe palette
ok <- c(POD = "#0072B2", FAR = "#D55E00", CSI = "#009E73",
        imerg = "#0072B2", cr2 = "#D55E00", cross = "#000000")

theme_pub <- theme_bw(base_size = 9) + theme(
  panel.grid.minor = element_blank(),
  panel.grid.major = element_line(linewidth = 0.2, colour = "grey90"),
  panel.border = element_rect(linewidth = 0.4, colour = "grey40"),
  axis.ticks = element_line(linewidth = 0.3, colour = "grey40"),
  legend.key.size = unit(0.8, "lines"),
  plot.margin = margin(4, 6, 4, 4)
)

## ── Fig 2 — I–D calibration sweep (lumped Maipo) ────────────────────────────
sw <- read.csv("data/fig_idsweep.csv")
swl <- data.frame(
  a = rep(sw$a, 3),
  metric = factor(rep(c("POD", "FAR", "CSI"), each = nrow(sw)), levels = c("POD", "FAR", "CSI")),
  value = c(sw$POD, sw$FAR, sw$CSI)
)
p2 <- ggplot(swl, aes(a, value, colour = metric)) +
  geom_vline(xintercept = 5.5, linetype = 2, colour = "grey55", linewidth = 0.4) +
  annotate("text", x = 5.5, y = 0.99, label = "a* = 5.5", hjust = -0.1, size = 2.6, colour = "grey35") +
  geom_line(linewidth = 0.6) + geom_point(size = 1.1) +
  scale_colour_manual(values = ok[c("POD", "FAR", "CSI")], name = NULL) +
  scale_x_continuous(breaks = seq(2, 16, 2)) +
  labs(x = expression("I–D intercept " * italic(a) * " (mm h"^{-1} * ")"), y = "score") +
  theme_pub + theme(legend.position = c(0.86, 0.78),
                    legend.background = element_rect(fill = "white", colour = "grey80", linewidth = 0.3))
ggsave(file.path(outdir, "fig2_idsweep.pdf"), p2, width = 3.3, height = 2.5, device = cairo_pdf)

## ── Fig 3 — sub-daily lead time: IMERG ½-hourly vs CR2MET daily ──────────────
## Numeric x = hours since 2015-03-24 00:00 UTC (robust vs datetime-scale quirks).
t0 <- as.POSIXct("2015-03-24 00:00", tz = "UTC")
im <- read.csv("data/atacama_imerg_hhr.csv")
im$h <- as.numeric(difftime(as.POSIXct(im$datetime, format = "%Y-%m-%dT%H:%M:%S", tz = "UTC"), t0, units = "hours"))
cr <- read.csv("data/atacama_cr2met_daily.csv")
cr$h0 <- as.numeric(difftime(as.POSIXct(paste0(cr$date, " 00:00"), format = "%Y-%m-%d %H:%M", tz = "UTC"), t0, units = "hours"))
imw <- subset(im, h >= 0 & h < 48)
crw <- subset(cr, h0 >= 0 & h0 < 48)
cross_h <- 5  # I–D crossing: 24-Mar 05:00 UTC

p3 <- ggplot() +
  geom_segment(data = crw, aes(x = h0, xend = h0 + 24, y = p_mm / 24, yend = p_mm / 24,
                               colour = "CR2MET daily (total/24 h)"), linewidth = 0.9) +
  geom_area(data = imw, aes(h, core_mm_hr), fill = ok[["imerg"]], alpha = 0.18) +
  geom_line(data = imw, aes(h, core_mm_hr, colour = "GPM IMERG ½-hourly"), linewidth = 0.5) +
  geom_vline(xintercept = cross_h, linetype = 2, colour = ok[["cross"]], linewidth = 0.4) +
  annotate("text", x = cross_h + 0.8, y = 36, label = "I–D crossing\n24-Mar 05:00 UTC", hjust = 0, size = 2.4, lineheight = 0.9) +
  scale_colour_manual(values = c("GPM IMERG ½-hourly" = ok[["imerg"]], "CR2MET daily (total/24 h)" = ok[["cr2"]]), name = NULL) +
  scale_x_continuous(breaks = c(0, 12, 24, 36, 48),
                     labels = c("24-Mar", "12 h", "25-Mar", "12 h", "26-Mar")) +
  labs(x = "time (UTC)", y = expression("rainfall intensity (mm h"^{-1} * ")")) +
  theme_pub + theme(legend.position = c(0.70, 0.85),
                    legend.background = element_rect(fill = "white", colour = "grey80", linewidth = 0.3))
ggsave(file.path(outdir, "fig3_leadtime.pdf"), p3, width = 4.6, height = 2.6, device = cairo_pdf)

## ── Fig 4 — distributed hazard map (Maipo, wettest step) ─────────────────────
hz <- read.csv("data/fig_hazard.csv")
p4 <- ggplot(hz, aes(lon, lat, fill = hazard)) +
  geom_raster() +
  scale_fill_viridis_c(option = "magma", name = "hazard", limits = c(0, max(hz$hazard))) +
  coord_quickmap(expand = FALSE) +
  labs(x = "lon (°)", y = "lat (°)") +
  theme_pub + theme(legend.position = "right", panel.grid.major = element_blank())
ggsave(file.path(outdir, "fig4_hazard.pdf"), p4, width = 3.4, height = 2.7, device = cairo_pdf)

cat("figures written to", outdir, "\n")
