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

## ── Fig 1 — study-area locator map (Chile) ──────────────────────────────────
suppressMessages({library(sf); library(rnaturalearth)})
sa <- ne_countries(continent = "South America", scale = "medium", returnclass = "sf")
sites <- data.frame(
  name = c("Atacama / Copiapó (2015)", "Río Maipo basin\n+ Cajón del Maipo (2017)",
           "Río Itata (flood gauge)", "Villa Santa Lucía (2017)"),
  lon  = c(-70.30, -70.20, -72.00, -72.30),
  lat  = c(-27.35, -33.65, -37.15, -43.40),
  type = c("Landslide / debris flow", "Landslide / debris flow",
           "Flood (discharge)", "Landslide / debris flow")
)
mapcol <- c("Landslide / debris flow" = "#D55E00", "Flood (discharge)" = "#0072B2")
p1 <- ggplot() +
  geom_sf(data = sa, fill = "grey92", colour = "grey70", linewidth = 0.25) +
  geom_point(data = sites, aes(lon, lat, colour = type, shape = type), size = 1.9) +
  geom_text(data = sites, aes(lon, lat, label = name), hjust = 1, nudge_x = -0.6,
            size = 2.2, lineheight = 0.85, colour = "grey15") +
  scale_colour_manual(values = mapcol, name = NULL) +
  scale_shape_manual(values = c("Landslide / debris flow" = 16, "Flood (discharge)" = 17), name = NULL) +
  coord_sf(xlim = c(-80, -65.5), ylim = c(-46.5, -25.5), expand = FALSE) +
  annotate("text", x = -76.0, y = -30, label = "Pacific\nOcean", size = 2.4,
           fontface = "italic", colour = "grey55", lineheight = 0.85) +
  annotate("text", x = -70.2, y = -25.0, label = "Chile", size = 3, colour = "grey35") +
  labs(x = NULL, y = NULL) +
  theme_pub + theme(legend.position = "bottom", legend.direction = "vertical",
                    panel.background = element_rect(fill = "white"),
                    legend.key.size = unit(0.7, "lines"), legend.text = element_text(size = 7),
                    legend.margin = margin(0, 0, 0, 0), legend.spacing.y = unit(1, "pt"))
ggsave(file.path(outdir, "fig1_studyarea.pdf"), p1, width = 3.0, height = 4.0, device = cairo_pdf)

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

## ── Fig (synthetic) — discrimination vs forcing resolution ──────────────────
## Controlled experiment: planted sub-daily bursts, identical field aggregated to
## coarser resolution. AUC and operational catch rate (POD@5%) vs resolution.
sr <- read.csv("data/synthetic_resolution.csv")
srl <- data.frame(
  dt = rep(sr$dt_h, 2),
  metric = factor(rep(c("ROC-AUC (× susc.)", "POD @ 5% area"), each = nrow(sr)),
                  levels = c("ROC-AUC (× susc.)", "POD @ 5% area")),
  value = c(sr$auc_realsusc_mean, sr$pod_mean),
  sd = c(sr$auc_realsusc_sd, sr$pod_sd)
)
psyn <- ggplot(srl, aes(dt, value, colour = metric, fill = metric, shape = metric)) +
  geom_hline(yintercept = 0.5, linetype = 3, colour = "grey60", linewidth = 0.35) +
  annotate("text", x = 0.5, y = 0.52, label = "AUC = 0.5 (random)", hjust = 0, vjust = 0,
           size = 2.3, colour = "grey45") +
  geom_ribbon(aes(ymin = value - sd, ymax = value + sd), colour = NA, alpha = 0.15) +
  geom_line(linewidth = 0.6) + geom_point(size = 1.4) +
  scale_colour_manual(values = c("ROC-AUC (× susc.)" = ok[["POD"]],
                                 "POD @ 5% area" = ok[["FAR"]]), name = NULL) +
  scale_fill_manual(values = c("ROC-AUC (× susc.)" = ok[["POD"]],
                               "POD @ 5% area" = ok[["FAR"]]), name = NULL) +
  scale_shape_manual(values = c(16, 17), name = NULL) +
  scale_x_log10(breaks = c(0.5, 1, 3, 6, 12, 24),
                labels = c("0.5", "1", "3", "6", "12", "24")) +
  scale_y_continuous(limits = c(0, 1), breaks = seq(0, 1, 0.2)) +
  labs(x = "forcing resolution (h, log scale)", y = "skill") +
  theme_pub + theme(legend.position = c(0.30, 0.22),
                    legend.background = element_rect(fill = "white", colour = "grey80", linewidth = 0.3))
ggsave(file.path(outdir, "fig_synthetic.pdf"), psyn, width = 3.4, height = 2.5, device = cairo_pdf)

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
